"""Verifying a platform member's `id_token` — the trust boundary between "a
code exchanged for a token" and "this platform will start a session for
whoever the IdP says signed in".

OIDC login design §2. Close cousin of `end_user_credential.verify` (an
*enterprise's* JWT, checked against a public key a workspace administrator
registered), but the shape of what is being checked differs in the two
places that matter: `aud` here is the platform's own `client_id`, never a
workspace id, and there is a `nonce` to check because the whole point of
generating one at `/start` (design §2, "校验 ... nonce") is to prove this
`id_token` was minted for *this* authorization request and not replayed from
one the IdP issued earlier for somebody else's.

Same discipline as `end_user_credential` otherwise, for the same reasons
that module's docstring gives: the algorithm allow-list is fixed here and
never taken from the token's own header, and `jwt.decode` is the one call
that checks a signature — nothing upstream of it is trusted with the
plaintext claims.

Pure: no database, no HTTP, no clock read from the wall. The caller has
already resolved the JWKS key (`identity/infrastructure/jwks_key_source.py`,
reused as-is) and already knows which `nonce` this login's `/start` minted.
"""

import hmac
from dataclasses import dataclass
from typing import Any, cast

import jwt

#: Fixed and spelled out here, never derived from the token's own header —
#: see `end_user_credential`'s module docstring for why that distinction is
#: the whole point: a decoder that trusts `alg` off the wire is a decoder an
#: attacker can steer into treating a public RSA key as an HMAC secret.
ALLOWED_ALGORITHMS: tuple[str, ...] = ("RS256", "ES256")

#: Claims read for a display name, tried in order. Cosmetic only — never an
#: identity key. `OidcProviderService` looks a user up and creates one by
#: `sub` alone; nothing here or downstream ever queries by this value, which
#: is what keeps it safe to fill from a claim the IdP asserts but never
#: verifies the ownership of the way it verifies `sub`.
_DISPLAY_NAME_CLAIMS: tuple[str, ...] = ("name", "email")


@dataclass(frozen=True)
class VerifiedOidcLogin:
    #: The IdP's own `sub` — opaque, and the only claim `OidcProviderService`
    #: is ever allowed to look an identity up by (OIDC login design's red
    #: line: never by email).
    subject: str
    display_name: str | None


@dataclass(frozen=True)
class OidcRefusal:
    """A token this function will not turn into a login.

    `reason` is for an audit record's own `context`, never for the HTTP
    response: `OidcProviderService` collapses every case in this file (bad
    signature, wrong `iss`, wrong `aud`, expired, malformed claims, wrong
    `nonce`) into the same generic refusal, for the same reason
    `end_user_credential`'s module docstring gives — an attacker probing this
    endpoint must not be able to tell "your nonce was wrong" from "your
    signature was wrong" apart.
    """

    reason: str


def verify_id_token(
    id_token: str,
    *,
    key_pem: str,
    issuer: str,
    client_id: str,
    nonce: str,
) -> VerifiedOidcLogin | OidcRefusal:
    try:
        claims = jwt.decode(
            id_token,
            key=key_pem,
            algorithms=list(ALLOWED_ALGORITHMS),
            issuer=issuer,
            audience=client_id,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError:
        # Bad signature, wrong `iss`/`aud`, expired, or a missing required
        # claim all raise a subclass of this and all mean the same thing to
        # a caller: no login.
        return OidcRefusal("claims")

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        return OidcRefusal("claims")

    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
        # §2's other CSRF/replay guard, alongside `state`: a signature-valid
        # `id_token` minted for a *different* authorization request must not
        # complete this one.
        return OidcRefusal("nonce")

    return VerifiedOidcLogin(sub.strip(), _display_name(claims))


def _display_name(claims: dict[str, Any]) -> str | None:
    for key in _DISPLAY_NAME_CLAIMS:
        value = cast(Any, claims.get(key))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
