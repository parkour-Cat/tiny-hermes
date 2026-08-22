"""`verify_id_token`: the one function that checks an `id_token`'s signature,
`iss`, `aud`, `exp`, and `nonce`. Pure, so every case here is a real signed
JWT and a real cryptographic check — no network, no clock read from the
wall (there is no injectable clock here, unlike `end_user_credential.verify`,
because this file enforces no platform-side lifetime ceiling of its own;
`exp` is checked by PyJWT against the real wall clock, which is why the
"expired" case below signs a token already in the past rather than pinning a
`now`)."""

from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from tiny_hermes.identity.domain.oidc import OidcRefusal, VerifiedOidcLogin, verify_id_token

ISSUER = "https://idp.acme.example"
CLIENT_ID = "platform-console"


def _rsa_keypair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


PRIVATE_PEM, PUBLIC_PEM = _rsa_keypair()
_OTHER_PRIVATE_PEM, OTHER_PUBLIC_PEM = _rsa_keypair()


def _claims(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "idp-user-42",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "nonce": "the-nonce",
    }
    base.update(overrides)
    return base


def _token(payload: dict[str, object], *, key: str = PRIVATE_PEM) -> str:
    return jwt.encode(payload, key, algorithm="RS256")


def _verify(token: str, *, nonce: str = "the-nonce") -> VerifiedOidcLogin | OidcRefusal:
    return verify_id_token(
        token, key_pem=PUBLIC_PEM, issuer=ISSUER, client_id=CLIENT_ID, nonce=nonce
    )


def test_a_well_formed_token_verifies() -> None:
    result = _verify(_token(_claims(name="Ada Lovelace")))

    assert isinstance(result, VerifiedOidcLogin)
    assert result.subject == "idp-user-42"
    assert result.display_name == "Ada Lovelace"


def test_display_name_falls_back_to_email_then_to_none() -> None:
    email_only = _verify(_token(_claims(email="ada@example.com")))
    assert isinstance(email_only, VerifiedOidcLogin)
    assert email_only.display_name == "ada@example.com"

    neither = _verify(_token(_claims()))
    assert isinstance(neither, VerifiedOidcLogin)
    assert neither.display_name is None


def test_a_bad_signature_is_refused() -> None:
    token = _token(_claims(), key=_OTHER_PRIVATE_PEM)

    result = _verify(token)

    assert isinstance(result, OidcRefusal)


def test_a_wrong_issuer_is_refused() -> None:
    result = _verify(_token(_claims(iss="https://not-the-idp.example")))

    assert isinstance(result, OidcRefusal)


def test_a_wrong_audience_is_refused() -> None:
    result = _verify(_token(_claims(aud="somebody-elses-client-id")))

    assert isinstance(result, OidcRefusal)


def test_an_expired_token_is_refused() -> None:
    now = datetime.now(UTC)
    expired = _claims(
        iat=int((now - timedelta(minutes=20)).timestamp()),
        exp=int((now - timedelta(minutes=10)).timestamp()),
    )

    result = _verify(_token(expired))

    assert isinstance(result, OidcRefusal)


def test_a_wrong_nonce_is_refused() -> None:
    result = _verify(_token(_claims(nonce="a-different-nonce")))

    assert isinstance(result, OidcRefusal)
    assert result.reason == "nonce"


def test_a_missing_nonce_is_refused() -> None:
    claims = _claims()
    del claims["nonce"]

    result = _verify(_token(claims))

    assert isinstance(result, OidcRefusal)


def test_a_missing_sub_is_refused() -> None:
    claims = _claims()
    del claims["sub"]

    result = _verify(_token(claims))

    assert isinstance(result, OidcRefusal)
