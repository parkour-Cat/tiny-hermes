"""Everything `verify` cannot see on its own, orchestrated into a session.

Design §4.2, §4.3, §5 (issuer registry only — the two-gate Agent check is a
later task), and §8. `verify` (`identity/domain/end_user_credential.py`) is
the whole trust boundary for one signed string; this module is what happens
before and after it — resolving which `channel_issuers` row a credential's
`iss` names, upserting `external_identities` (§282's idempotent identity),
and turning the result into a session this platform's own cookie can carry.

**The unregistered-issuer fix lives here, not in `verify`.** That module's
own docstring says why: a disabled issuer still pays for a real signature
check, so refusing it never answers faster than a bad signature on an active
one — but a *lookup miss* has no row and therefore no key to check anything
against, and a naive "no row, no work" short-circuit would answer faster than
every registered issuer, active or not. That gap is a probe an attacker could
use to enumerate which issuers a workspace has configured, which is exactly
what §8 says a refusal must never leak. `exchange` closes it by running
`verify` against a fixed, generated-once dummy key on every miss — the
private half was discarded the moment the key pair was made and is not
anywhere in this repository or its history — so a miss costs a real
asymmetric signature check, the same as every other path.
"""

import base64
import hashlib
import json
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

import jwt

from tiny_hermes.identity.domain.end_user_credential import (
    IssuerRecord,
    Refusal,
    RefusalReason,
    VerifiedCredential,
    verify,
)
from tiny_hermes.identity.domain.models import ChannelIssuerStatus
from tiny_hermes.identity.ports.end_user_keys import EndUserKeySource
from tiny_hermes.identity.ports.end_user_store import (
    ChannelIssuerRecord,
    EndUserStore,
    IssuerAlreadyRegistered,
)
from tiny_hermes.tenancy.domain.models import Actor, Role

#: A key pair generated once for this file and never used to sign anything
#: real. Its only job is to give `jwt.decode` an asymmetric signature to
#: check on a lookup miss, so "no row" and "wrong row" cost the same (module
#: docstring). Fixed rather than generated per call: generating a fresh RSA
#: key on every miss would itself cost more than the check it stands in for,
#: and would make the miss path's own cost vary run to run.
_DUMMY_RSA_PUBLIC_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAwfwKCmKk9vvwGnFOdjv6\n"
    "bz3D5SH8nwqgoLxAnv9zIP8wmEvcLb4dlZdLRzkKC56w7/ZgtKVbr+vHpNKZ+J2y\n"
    "0Esm8RTPbFFl/hOwdP57ViQW4RsdO43be6WISAQ3xCWz2ThaPyaQmWBxRoVqIWRx\n"
    "hQajubRHj4SHKFSsEpzKTjOqNXsRWnISkdAidmDcV+gH0t9oT2ohN3OhENo+l069\n"
    "XAUE3zMschoPtIY3lj8T01b1+jUdh3CIzKnO2YOgLuBA2T8ulalvyJWBg3jKPHMn\n"
    "8p+cEGzen/mNtIyEumwr5CCBXP0vmKchQwyAMUpqzkP725oDo5RLb5p/5LDJeqxM\n"
    "3QIDAQAB\n"
    "-----END PUBLIC KEY-----\n"
)
_DUMMY_EC_PUBLIC_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAELeNPHNJ524/1Msuh7gl/hLQRPJwv\n"
    "RkBXNVXgdI9nnvHuznwgedRAOaMUWZUsGAM1M9agW5hrvkkOVkuJim4MPg==\n"
    "-----END PUBLIC KEY-----\n"
)
#: An issuer name a real `channel_issuers` row can never carry, since it is
#: not a URL any enterprise IdP would use. Never asserted against on the
#: verifying side — `verify`'s signature check runs before its `iss` check
#: either way (PyJWT decodes and verifies the signature before validating
#: claims), so this only needs to be a value, not a working credential.
_UNREGISTERED_ISSUER = "urn:tiny-hermes:unregistered"

WRITERS = frozenset({Role.WORKSPACE_ADMIN})


class ForbiddenEndUserAction(Exception):
    pass


class UnknownChannelIssuer(Exception):
    pass


class UnknownEndUser(Exception):
    pass


class InvalidChannelIssuer(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CredentialExchangeRefused(Exception):
    """§8's refusal table, carried up from `verify` (or raised here directly
    for the two checks `verify` cannot make on its own: issuer lookup and
    subject erasure)."""

    def __init__(self, reason: RefusalReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class ExchangedSession:
    end_user_id: UUID
    session_token: str
    expires_at: datetime


@dataclass(frozen=True)
class EndUserSession:
    end_user_id: UUID
    workspace_id: UUID


class EndUserIdentityService:
    def __init__(
        self, store: EndUserStore, keys: EndUserKeySource, session_ttl: timedelta
    ) -> None:
        self._store = store
        self._keys = keys
        self._session_ttl = session_ttl

    @staticmethod
    def digest_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    # -- credential exchange, design §4.2 --------------------------------

    async def exchange(
        self, token: str, workspace_id: UUID, now: datetime, request_id: str
    ) -> ExchangedSession:
        alg = _peek_alg(token)
        iss = _peek_issuer(token)
        issuer_row = await self._store.find_issuer(workspace_id, iss) if iss else None

        if issuer_row is None:
            # Pay for the signature check a real issuer would cost, then
            # refuse exactly like every other case — see the module
            # docstring for why this branch exists at all.
            verify(token, _dummy_issuer_record(workspace_id, alg), now)
            raise CredentialExchangeRefused(RefusalReason.INVALID)

        resolved_key = await self._keys.resolve(
            public_key=issuer_row.public_key, jwks_url=issuer_row.jwks_url, token=token
        )
        issuer_record = IssuerRecord(
            issuer=issuer_row.issuer,
            workspace_id=workspace_id,
            public_key=resolved_key or _dummy_key_for(alg),
            status=issuer_row.status,
        )
        result = verify(token, issuer_record, now)
        if isinstance(result, Refusal):
            raise CredentialExchangeRefused(result.reason)

        identity = await self._store.upsert_external_identity(
            workspace_id, issuer_row.channel, result.external_user_id
        )
        if identity.erased_at is not None:
            # design §8: an erased subject's credential must not exchange for
            # a session, collapsed into the same generic refusal as every
            # other case rather than distinguished — an erased subject is not
            # something worth confirming to whoever is holding the token.
            raise CredentialExchangeRefused(RefusalReason.INVALID)

        session_token = secrets.token_urlsafe(32)
        expires_at = now + self._session_ttl
        await self._store.create_session(
            identity.end_user_id, workspace_id, self.digest_token(session_token), expires_at
        )
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_type="end_user",
            actor_id=identity.end_user_id,
            action="end_user.session_exchanged",
            resource_type="end_user_session",
            resource_id=identity.end_user_id,
            request_id=request_id,
            context={"channel": issuer_row.channel},
        )
        return ExchangedSession(identity.end_user_id, session_token, expires_at)

    async def authenticate(self, session_token: str, now: datetime) -> EndUserSession | None:
        stored = await self._store.find_session(self.digest_token(session_token), now)
        if stored is None:
            return None
        return EndUserSession(stored.end_user_id, stored.workspace_id)

    # -- revocation, design §4.3 ------------------------------------------

    async def revoke_sessions(
        self,
        actor: Actor,
        workspace_id: UUID,
        end_user_id: UUID,
        request_id: str,
        now: datetime,
    ) -> None:
        await self._require_admin(actor, workspace_id)
        if not await self._store.end_user_exists(workspace_id, end_user_id):
            raise UnknownEndUser
        await self._store.revoke_sessions(end_user_id, workspace_id, now)
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_type="user",
            actor_id=actor.id,
            action="end_user.sessions_revoked",
            resource_type="end_user",
            resource_id=end_user_id,
            request_id=request_id,
            context={},
        )

    # -- issuer registry, design §3 ---------------------------------------

    async def register_issuer(
        self,
        actor: Actor,
        workspace_id: UUID,
        *,
        channel: str,
        issuer: str,
        public_key: str | None,
        jwks_url: str | None,
        allowed_origins: Sequence[str],
        request_id: str,
    ) -> ChannelIssuerRecord:
        await self._require_admin(actor, workspace_id)
        normalized_channel = channel.strip().lower()
        normalized_issuer = issuer.strip()
        if not normalized_channel or not normalized_issuer:
            raise InvalidChannelIssuer("channel and issuer are both required")
        if not public_key and not jwks_url:
            raise InvalidChannelIssuer(
                "a channel issuer needs a public key or a JWKS URL"
            )
        if public_key and jwks_url:
            raise InvalidChannelIssuer(
                "a channel issuer takes a public key or a JWKS URL, not both"
            )
        try:
            record = await self._store.create_issuer(
                workspace_id=workspace_id,
                channel=normalized_channel,
                issuer=normalized_issuer,
                public_key=public_key,
                jwks_url=jwks_url,
                allowed_origins=tuple(allowed_origins),
                created_by=actor.id,
            )
        except IssuerAlreadyRegistered:
            raise
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_type="user",
            actor_id=actor.id,
            action="end_user.issuer_registered",
            resource_type="channel_issuer",
            resource_id=record.id,
            request_id=request_id,
            context={"channel": normalized_channel, "issuer": normalized_issuer},
        )
        return record

    async def list_issuers(
        self, actor: Actor, workspace_id: UUID
    ) -> Sequence[ChannelIssuerRecord]:
        await self._require_reader(actor, workspace_id)
        return await self._store.list_issuers(workspace_id)

    async def disable_issuer(
        self, actor: Actor, workspace_id: UUID, issuer_id: UUID, request_id: str
    ) -> ChannelIssuerRecord:
        await self._require_admin(actor, workspace_id)
        record = await self._store.disable_issuer(workspace_id, issuer_id)
        if record is None:
            raise UnknownChannelIssuer
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_type="user",
            actor_id=actor.id,
            action="end_user.issuer_disabled",
            resource_type="channel_issuer",
            resource_id=issuer_id,
            request_id=request_id,
            context={},
        )
        return record

    async def _require_admin(self, actor: Actor, workspace_id: UUID) -> None:
        if actor.is_service_account:
            raise ForbiddenEndUserAction
        role = await self._store.user_role(workspace_id, actor.id)
        if role not in WRITERS:
            raise ForbiddenEndUserAction

    async def _require_reader(self, actor: Actor, workspace_id: UUID) -> None:
        if actor.is_service_account:
            raise ForbiddenEndUserAction
        role = await self._store.user_role(workspace_id, actor.id)
        if role is None:
            raise ForbiddenEndUserAction


def _dummy_key_for(alg: str | None) -> str:
    return _DUMMY_EC_PUBLIC_PEM if alg == "ES256" else _DUMMY_RSA_PUBLIC_PEM


def _dummy_issuer_record(workspace_id: UUID, alg: str | None) -> IssuerRecord:
    return IssuerRecord(
        issuer=_UNREGISTERED_ISSUER,
        workspace_id=workspace_id,
        public_key=_dummy_key_for(alg),
        status=ChannelIssuerStatus.ACTIVE,
    )


def _peek_alg(token: str) -> str | None:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None
    alg = header.get("alg")
    return alg if isinstance(alg, str) else None


def _peek_issuer(token: str) -> str | None:
    """The `iss` claim, read without verifying anything — this is what picks
    *which* row to check the credential against, so it necessarily runs
    before there is a key to verify with. Cheap by construction (no
    cryptography touched), so it is not part of the cost §8 cares about
    keeping constant; that cost starts at `verify`.

    Deliberately not `jwt.decode(..., options={"verify_signature": False})`:
    that still calls into `jwt.decode`, the same name `verify` calls for the
    real, expensive check, and the test that proves an unregistered issuer
    pays full price spies on exactly that name — a peek that shared it would
    inflate the count and hide a real regression behind a false one. Reading
    the payload segment by hand keeps the peek and the check unambiguously
    two different calls.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded: object = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    payload = cast(dict[str, object], decoded)
    iss = payload.get("iss")
    return iss if isinstance(iss, str) and iss.strip() else None


__all__ = [
    "ChannelIssuerRecord",
    "CredentialExchangeRefused",
    "EndUserIdentityService",
    "EndUserSession",
    "ExchangedSession",
    "ForbiddenEndUserAction",
    "InvalidChannelIssuer",
    "UnknownChannelIssuer",
    "UnknownEndUser",
    "VerifiedCredential",
]
