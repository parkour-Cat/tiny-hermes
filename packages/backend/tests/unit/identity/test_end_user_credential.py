"""`verify` turning a bearer credential into a subject, or a named refusal.

Design §4.1 and §8. Every claim gets its own test because the fix for a bad
signature, a stale `channel_issuers` row, and a misconfigured `exp` land with
different people — collapsing them into one test would hide which of the
three broke.

Two properties get extra scrutiny because they are the ones a reviewer would
distrust on sight rather than take on faith:

* the algorithm allow-list is enforced by `verify` itself, not by whatever the
  token's own header claims — proved by re-signing a token with HMAC using the
  RSA public key as the secret, and by an `alg: none` token, both refused;
* the platform's 15-minute ceiling on `exp` returns a refusal reason that is
  distinguishable from every other failure, because it is the one failure
  worth telling an enterprise about, while the rest deliberately collapse into
  one 401 so nobody can use the response to probe which check failed.
"""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from tiny_hermes.identity.domain.end_user_credential import (
    IssuerRecord,
    Refusal,
    RefusalReason,
    VerifiedCredential,
    verify,
)
from tiny_hermes.identity.domain.models import ChannelIssuerStatus

ISSUER = "https://idp.acme.example"
WORKSPACE_ID: UUID = uuid4()
NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


def _rsa_keypair() -> tuple[str, str]:
    """(private_pem, public_pem) for RS256 tokens."""
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


def _ec_keypair() -> tuple[str, str]:
    """(private_pem, public_pem) for ES256 tokens."""
    private_key = ec.generate_private_key(ec.SECP256R1())
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


RSA_PRIVATE_PEM, RSA_PUBLIC_PEM = _rsa_keypair()
OTHER_RSA_PRIVATE_PEM, _OTHER_RSA_PUBLIC_PEM = _rsa_keypair()
EC_PRIVATE_PEM, EC_PUBLIC_PEM = _ec_keypair()


def issuer_record(
    *,
    public_key: str = RSA_PUBLIC_PEM,
    status: ChannelIssuerStatus = ChannelIssuerStatus.ACTIVE,
    issuer: str = ISSUER,
    workspace_id: UUID = WORKSPACE_ID,
) -> IssuerRecord:
    return IssuerRecord(
        issuer=issuer,
        workspace_id=workspace_id,
        public_key=public_key,
        status=status,
    )


def claims(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "iss": ISSUER,
        "sub": "acme-user-42",
        "aud": str(WORKSPACE_ID),
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(minutes=10)).timestamp()),
        "agents": ["support-bot"],
    }
    base.update(overrides)
    return base


def rs256(payload: dict[str, object], *, key: str = RSA_PRIVATE_PEM) -> str:
    return jwt.encode(payload, key, algorithm="RS256")


def es256(payload: dict[str, object], *, key: str = EC_PRIVATE_PEM) -> str:
    return jwt.encode(payload, key, algorithm="ES256")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forged_hmac_token(payload: dict[str, object], secret: str) -> str:
    """Hand-built rather than `jwt.encode(..., algorithm="HS256")`: PyJWT's
    own `prepare_key` refuses to sign with a PEM-shaped secret, which is
    exactly the defense this test exists to check for on the *verifying*
    side. An attacker forging this token would not go through PyJWT's guard
    rail either — they would build the three segments by hand, which is what
    this does."""
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = (
        f"{_b64url(json.dumps(header).encode())}."
        f"{_b64url(json.dumps(payload).encode())}"
    )
    signature = hmac.new(
        secret.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url(signature)}"


# -- happy path, both algorithms on the allow-list ---------------------------


def test_a_well_formed_rs256_credential_verifies() -> None:
    token = rs256(claims())

    result = verify(token, issuer_record(), NOW)

    assert result == VerifiedCredential(
        issuer=ISSUER,
        workspace_id=WORKSPACE_ID,
        external_user_id="acme-user-42",
        agents=("support-bot",),
    )


def test_a_well_formed_es256_credential_verifies() -> None:
    token = es256(claims())

    result = verify(token, issuer_record(public_key=EC_PUBLIC_PEM), NOW)

    assert isinstance(result, VerifiedCredential)
    assert result.external_user_id == "acme-user-42"


def test_a_credential_with_no_agents_claim_grants_none() -> None:
    payload = claims()
    del payload["agents"]
    token = rs256(payload)

    result = verify(token, issuer_record(), NOW)

    assert isinstance(result, VerifiedCredential)
    assert result.agents == ()


# -- signature ----------------------------------------------------------------


def test_a_credential_signed_by_the_wrong_key_is_refused() -> None:
    token = rs256(claims(), key=OTHER_RSA_PRIVATE_PEM)

    result = verify(token, issuer_record(), NOW)

    assert result == Refusal(RefusalReason.INVALID)


# -- alg confusion, the two cases the brief calls out by name -----------------


def test_a_token_resigned_with_hmac_using_the_public_key_as_secret_is_refused() -> None:
    """The classic alg-confusion attack: an RS256 verifier that trusts the
    token's own header will treat the public key as an HMAC secret and accept
    a token anyone with that (public!) key could forge. `verify` must never
    reach that branch because the algorithm allow-list is fixed by the caller,
    not read from the token."""
    token = forged_hmac_token(claims(), secret=RSA_PUBLIC_PEM)

    result = verify(token, issuer_record(), NOW)

    assert result == Refusal(RefusalReason.INVALID)


def test_an_alg_none_token_is_refused() -> None:
    token = jwt.encode(claims(), key="", algorithm="none")

    result = verify(token, issuer_record(), NOW)

    assert result == Refusal(RefusalReason.INVALID)


# -- iss / aud ------------------------------------------------------------


def test_a_credential_from_an_unrecognized_issuer_is_refused() -> None:
    token = rs256(claims(iss="https://not-acme.example"))

    result = verify(token, issuer_record(), NOW)

    assert result == Refusal(RefusalReason.INVALID)


def test_a_credential_audienced_to_another_workspace_is_refused() -> None:
    token = rs256(claims(aud=str(uuid4())))

    result = verify(token, issuer_record(), NOW)

    assert result == Refusal(RefusalReason.INVALID)


# -- exp / nbf --------------------------------------------------------------


def test_an_expired_credential_is_refused() -> None:
    token = rs256(
        claims(
            iat=int((NOW - timedelta(minutes=20)).timestamp()),
            exp=int((NOW - timedelta(minutes=1)).timestamp()),
        )
    )

    result = verify(token, issuer_record(), NOW)

    assert result == Refusal(RefusalReason.INVALID)


def test_a_credential_expired_by_one_second_is_refused() -> None:
    """No grace period on `exp`: design §4.1 grants the 60-second clock skew
    to `nbf` and `iat` only, and §8 lists expiry as a flat refusal. A token
    one second past `exp` sits well inside the 60-second skew window, so this
    is the case that catches the skew leaking onto the wrong claim."""
    token = rs256(
        claims(
            iat=int((NOW - timedelta(minutes=20)).timestamp()),
            exp=int((NOW - timedelta(seconds=1)).timestamp()),
        )
    )

    result = verify(token, issuer_record(), NOW)

    assert result == Refusal(RefusalReason.INVALID)


def test_a_credential_not_yet_valid_is_refused() -> None:
    token = rs256(claims(nbf=int((NOW + timedelta(minutes=5)).timestamp())))

    result = verify(token, issuer_record(), NOW)

    assert result == Refusal(RefusalReason.INVALID)


def test_nbf_within_the_60_second_clock_skew_still_verifies() -> None:
    token = rs256(claims(nbf=int((NOW + timedelta(seconds=30)).timestamp())))

    result = verify(token, issuer_record(), NOW)

    assert isinstance(result, VerifiedCredential)


# -- the platform's 15-minute ceiling, and that it is distinguishable --------


def test_exp_past_the_15_minute_ceiling_is_refused_with_a_distinguishable_reason() -> None:
    """A misconfiguration worth telling the enterprise about, per design §8 —
    unlike every other failure in this file, which must not say more than
    'invalid'."""
    token = rs256(
        claims(
            iat=int(NOW.timestamp()),
            exp=int((NOW + timedelta(hours=24)).timestamp()),
        )
    )

    result = verify(token, issuer_record(), NOW)

    assert result == Refusal(RefusalReason.LIFETIME_EXCEEDS_PLATFORM_CEILING)
    assert result != Refusal(RefusalReason.INVALID)


def test_exp_exactly_at_the_15_minute_ceiling_still_verifies() -> None:
    token = rs256(
        claims(
            iat=int(NOW.timestamp()),
            exp=int((NOW + timedelta(minutes=15)).timestamp()),
        )
    )

    result = verify(token, issuer_record(), NOW)

    assert isinstance(result, VerifiedCredential)


# -- issuer status ------------------------------------------------------------


def test_a_credential_from_a_disabled_issuer_is_refused() -> None:
    token = rs256(claims())

    result = verify(
        token, issuer_record(status=ChannelIssuerStatus.DISABLED), NOW
    )

    assert result == Refusal(RefusalReason.INVALID)


def test_a_disabled_issuer_still_pays_for_signature_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§8 wants a disabled/unregistered issuer indistinguishable from every
    other refusal, specifically so an attacker cannot probe which issuers
    exist. A short-circuit that refuses a disabled issuer *before*
    `jwt.decode` runs answers faster than a bad-signature refusal on an
    active issuer, which is exactly the timing side-channel that leaks it.
    `jwt.decode` — the expensive asymmetric verification — must run before
    the disabled-issuer refusal is returned, same as every other path."""
    token = rs256(claims())
    calls: list[str] = []
    real_decode = jwt.decode

    def spy_decode(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append("decoded")
        return real_decode(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "tiny_hermes.identity.domain.end_user_credential.jwt.decode", spy_decode
    )

    result = verify(
        token, issuer_record(status=ChannelIssuerStatus.DISABLED), NOW
    )

    assert result == Refusal(RefusalReason.INVALID)
    assert calls == ["decoded"]


# -- malformed claims ---------------------------------------------------------


def test_a_credential_missing_sub_is_refused() -> None:
    payload = claims()
    del payload["sub"]
    token = rs256(payload)

    result = verify(token, issuer_record(), NOW)

    assert result == Refusal(RefusalReason.INVALID)


def test_a_credential_whose_agents_claim_is_not_a_list_is_refused() -> None:
    token = rs256(claims(agents="support-bot"))

    result = verify(token, issuer_record(), NOW)

    assert result == Refusal(RefusalReason.INVALID)


@pytest.mark.parametrize("garbage", ["not-a-token", "", "a.b"])
def test_unparseable_input_is_refused(garbage: str) -> None:
    result = verify(garbage, issuer_record(), NOW)

    assert result == Refusal(RefusalReason.INVALID)
