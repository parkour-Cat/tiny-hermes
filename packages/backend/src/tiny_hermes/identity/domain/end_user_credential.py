"""Turning an enterprise-signed credential into a subject, or a named refusal.

Design §4.1 and §8. The platform is not an identity provider (§4.5.1) — an
end user never authenticates to it. What it verifies instead is a short-lived
JWT the *enterprise* signed, asserting who this person is and which Agents
their own employer has decided they may reach. `verify` is the whole trust
boundary between "a bearer string arrived over HTTP" and "this platform will
act as if a real subject sent it", so it does exactly one thing and refuses
everything else.

Pure, like every other domain module here: no database, no HTTP, no clock
read from the wall. `now` arrives as an argument rather than
`datetime.now()` for the same reason PyJWT's own `verify_exp`/`verify_nbf`
are turned off below and re-implemented against it — a function that reads
the real clock cannot be pinned to a moment in a test, and every claim in
this file that depends on time (`exp`, `nbf`, `iat`, and the 15-minute
ceiling) needs to be pinned to prove.

**The algorithm allow-list is the load-bearing line in this module.** A JWT's
header names its own algorithm, and a decoder that trusts that header is a
decoder an attacker can steer: hand it a token "signed" with HMAC using the
issuer's own *public* RSA key as the secret, and a verifier that reads
`alg: HS256` off the header and looks up an HMAC-compatible check will accept
it, because the public key is, of course, public. The fix is not a smarter
parser — it is never asking the token what algorithm to use. `algorithms=`
below is the fixed pair this platform accepts, spelled out at the one call
site that can check a signature, and nothing about a token's own claims ever
touches that list.

**Every failure returns `Refusal(RefusalReason.INVALID)` except one.** A bad
signature, a wrong `iss`, a wrong `aud`, an expired token, a token not yet
valid, a malformed claim, a disabled issuer — an attacker who can tell those
apart by the response has a way to enumerate a workspace's configuration one
probe at a time, so §8 collapses all of them into the same answer. The one
exception is `LIFETIME_EXCEEDS_PLATFORM_CEILING`: an `exp` set further out
than the platform allows is not something an attacker discovers by probing,
it is a mistake in the enterprise's own signer, and it is the one thing in
this list actually worth telling them.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

import jwt

from tiny_hermes.identity.domain.models import ChannelIssuerStatus

#: §4.1: "RS256 或 ES256". Passed explicitly to every `jwt.decode` call below
#: and never derived from the token — see the module docstring for why that
#: distinction is the whole point of this file.
ALLOWED_ALGORITHMS: tuple[str, ...] = ("RS256", "ES256")

#: §4.1's platform-enforced cap: a credential's `exp` may not sit more than
#: this far past the moment it is verified. Not derived from the credential's
#: own `iat` — an enterprise that mislabels `iat` to make a long-lived token
#: look freshly issued must not be able to launder it past this check, so the
#: ceiling is measured from `now`, the one clock this function is given and
#: cannot be told a story about.
MAX_CREDENTIAL_LIFETIME = timedelta(minutes=15)

#: §4.1: "允许 60 秒时钟偏移" for `nbf` and `iat`. Not applied to the ceiling
#: above — that is a platform policy, not a tolerance for drifting clocks.
CLOCK_SKEW = timedelta(seconds=60)


class RefusalReason(StrEnum):
    """One indistinguishable reason, and the one exception (module docstring)."""

    INVALID = "end_user_credential_invalid"
    LIFETIME_EXCEEDS_PLATFORM_CEILING = (
        "end_user_credential_lifetime_exceeds_platform_ceiling"
    )


@dataclass(frozen=True)
class Refusal:
    """A credential this function will not turn into a subject.

    A value, not an exception: `verify` has exactly one caller-visible
    failure shape, and a union return says that at the type level instead of
    asking every caller to remember a `try/except`.
    """

    reason: RefusalReason


@dataclass(frozen=True)
class IssuerRecord:
    """The slice of a `channel_issuers` row (design §3) this function needs.

    Not the row itself — that would pull SQLAlchemy into a domain module for
    no reason this function has. The caller has already resolved whichever of
    `public_key` / `jwks_url` that row carries down to one PEM-encoded key
    (fetching a JWKS is I/O this module does not do), and has already looked
    the row up scoped to one workspace, which is what lets `aud` be checked
    against `workspace_id` here instead of as a separate argument threaded
    through from an HTTP header.
    """

    issuer: str
    workspace_id: UUID
    public_key: str
    status: ChannelIssuerStatus


@dataclass(frozen=True)
class VerifiedCredential:
    """A credential that passed every check in this file.

    `agents` is the enterprise's own half of §5's two-gate check — the
    aliases this end user's employer decided they may reach. Whether the
    workspace's own `AgentSpec.end_user_access` gate agrees is the other
    half, and it is not this module's business: `verify` only reports what
    the credential itself asserted.
    """

    issuer: str
    workspace_id: UUID
    external_user_id: str
    agents: tuple[str, ...]


def _refused() -> Refusal:
    return Refusal(RefusalReason.INVALID)


def _as_datetime(value: object) -> datetime | None:
    """A JWT numeric-date claim, or `None` if it is not one.

    `bool` is excluded before `int`/`float` because `isinstance(True, int)`
    is true in Python, and `True` was never a timestamp.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def _agents_of(value: object) -> tuple[str, ...] | None:
    """§4.1's `agents` claim, or `None` if the token sent something that is
    not an array of names. Missing entirely is not this case — the caller
    passes `[]` for that, which becomes "no Agents assigned" rather than a
    refusal; a credential that never mentions `agents` has not misconfigured
    anything, it has simply not delegated any access yet.
    """
    if not isinstance(value, list):
        return None
    agents: list[str] = []
    for item in cast(list[Any], value):
        if not isinstance(item, str) or not item.strip():
            return None
        agents.append(item.strip())
    return tuple(agents)


def verify(
    token: str, issuer_record: IssuerRecord, now: datetime
) -> VerifiedCredential | Refusal:
    """The whole check, claim by claim, or the one refusal that covers all
    of them but one (module docstring)."""
    if issuer_record.status is not ChannelIssuerStatus.ACTIVE:
        # §4.3: disabling a row invalidates its issuer's *new* credentials
        # immediately. A token that verifies structurally against a disabled
        # row is exactly the credential this check exists to catch.
        return _refused()

    try:
        claims = jwt.decode(
            token,
            key=issuer_record.public_key,
            algorithms=list(ALLOWED_ALGORITHMS),
            issuer=issuer_record.issuer,
            audience=str(issuer_record.workspace_id),
            options={
                "require": ["exp", "sub"],
                # Re-checked below against the injected `now` instead of
                # PyJWT's own wall-clock read — see the module docstring.
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError:
        # Bad signature, wrong `iss`, wrong `aud`, or a missing required
        # claim all raise a subclass of this and all mean the same thing to
        # a caller: no subject.
        return _refused()

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        return _refused()

    exp_at = _as_datetime(claims.get("exp"))
    if exp_at is None:
        return _refused()

    if "nbf" in claims:
        nbf_at = _as_datetime(claims.get("nbf"))
        if nbf_at is None or nbf_at > now + CLOCK_SKEW:
            return _refused()

    if "iat" in claims:
        iat_at = _as_datetime(claims.get("iat"))
        if iat_at is None or iat_at > now + CLOCK_SKEW:
            return _refused()

    if exp_at <= now - CLOCK_SKEW:
        return _refused()

    if exp_at - now > MAX_CREDENTIAL_LIFETIME:
        # The one distinguishable refusal in this file (module docstring).
        return Refusal(RefusalReason.LIFETIME_EXCEEDS_PLATFORM_CEILING)

    agents = _agents_of(claims.get("agents", []))
    if agents is None:
        return _refused()

    return VerifiedCredential(
        issuer=issuer_record.issuer,
        workspace_id=issuer_record.workspace_id,
        external_user_id=sub.strip(),
        agents=agents,
    )
