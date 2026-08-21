"""`EndUserIdentityService`: orchestrating `verify` into a session, or a
named refusal, without a database.

Design §4.2, §4.3, and §8. `verify` (tested exhaustively in
`test_end_user_credential.py`) is used here for real, with real signed
tokens — what is new at this layer is everything `verify` cannot see on its
own: which `channel_issuers` row a credential's `iss` resolves to, what
happens when none does, idempotent upsert into `external_identities`, and
turning a `VerifiedCredential` into a session a cookie can carry.

The fixed-cost refusal for an unregistered issuer (carried forward from the
previous task's review) is proved the same way `end_user_credential.py`
proves it for a disabled one: a spy on `jwt.decode` shows the expensive
signature check ran, not a wall-clock measurement.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from tiny_hermes.identity.application.end_user_service import (
    CredentialExchangeRefused,
    EndUserIdentityService,
    EndUserSession,
    ForbiddenEndUserAction,
    InvalidChannelIssuer,
    UnknownChannelIssuer,
)
from tiny_hermes.identity.domain.end_user_credential import RefusalReason
from tiny_hermes.identity.domain.models import ChannelIssuerStatus
from tiny_hermes.identity.ports.end_user_store import (
    ChannelIssuerRecord,
    StoredEndUserSession,
    UpsertedIdentity,
)
from tiny_hermes.tenancy.domain.models import Actor, Role

ISSUER = "https://idp.acme.example"
CHANNEL = "web"
WORKSPACE_ID: UUID = uuid4()
NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


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


RSA_PRIVATE_PEM, RSA_PUBLIC_PEM = _rsa_keypair()
_OTHER_PRIVATE_PEM, OTHER_PUBLIC_PEM = _rsa_keypair()


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


@dataclass
class FakeEndUserStore:
    """In-memory stand-in for `EndUserStore`. Keyed the way the real tables
    are: issuers by `(workspace_id, issuer)`, identities by
    `(workspace_id, channel, external_user_id)`, sessions by digest."""

    issuers: dict[tuple[UUID, str], ChannelIssuerRecord] = field(
        default_factory=dict[tuple[UUID, str], ChannelIssuerRecord]
    )
    identities: dict[tuple[UUID, str, str], UUID] = field(
        default_factory=dict[tuple[UUID, str, str], UUID]
    )
    erased: set[UUID] = field(default_factory=set[UUID])
    sessions: dict[str, StoredEndUserSession] = field(
        default_factory=dict[str, StoredEndUserSession]
    )
    revoked: set[str] = field(default_factory=set[str])
    memberships: dict[tuple[UUID, UUID], Role] = field(
        default_factory=dict[tuple[UUID, UUID], Role]
    )
    audits: list[dict[str, object]] = field(default_factory=list[dict[str, object]])

    def register(
        self,
        *,
        workspace_id: UUID = WORKSPACE_ID,
        channel: str = CHANNEL,
        issuer: str = ISSUER,
        public_key: str | None = RSA_PUBLIC_PEM,
        jwks_url: str | None = None,
        status: ChannelIssuerStatus = ChannelIssuerStatus.ACTIVE,
        allowed_origins: tuple[str, ...] = (),
    ) -> ChannelIssuerRecord:
        record = ChannelIssuerRecord(
            id=uuid4(),
            workspace_id=workspace_id,
            channel=channel,
            issuer=issuer,
            public_key=public_key,
            jwks_url=jwks_url,
            allowed_origins=allowed_origins,
            status=status,
            created_by=uuid4(),
            created_at=NOW,
        )
        self.issuers[(workspace_id, issuer)] = record
        return record

    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        return self.memberships.get((workspace_id, user_id))

    async def active_allowed_origins(self, workspace_id: UUID) -> frozenset[str]:
        origins: set[str] = set()
        for (w, _), record in self.issuers.items():
            if w == workspace_id and record.status is ChannelIssuerStatus.ACTIVE:
                origins.update(record.allowed_origins)
        return frozenset(origins)

    async def create_issuer(
        self,
        *,
        workspace_id: UUID,
        channel: str,
        issuer: str,
        public_key: str | None,
        jwks_url: str | None,
        allowed_origins: Sequence[str],
        created_by: UUID,
    ) -> ChannelIssuerRecord:
        return self.register(
            workspace_id=workspace_id,
            channel=channel,
            issuer=issuer,
            public_key=public_key,
            jwks_url=jwks_url,
            allowed_origins=tuple(allowed_origins),
        )

    async def list_issuers(self, workspace_id: UUID) -> list[ChannelIssuerRecord]:
        return [r for (w, _), r in self.issuers.items() if w == workspace_id]

    async def disable_issuer(
        self, workspace_id: UUID, issuer_id: UUID
    ) -> ChannelIssuerRecord | None:
        for key, record in self.issuers.items():
            if record.id == issuer_id and key[0] == workspace_id:
                disabled = replace(record, status=ChannelIssuerStatus.DISABLED)
                self.issuers[key] = disabled
                return disabled
        return None

    async def find_issuer(self, workspace_id: UUID, issuer: str) -> ChannelIssuerRecord | None:
        return self.issuers.get((workspace_id, issuer))

    async def upsert_external_identity(
        self, workspace_id: UUID, channel: str, external_user_id: str
    ) -> UpsertedIdentity:
        key = (workspace_id, channel, external_user_id)
        end_user_id = self.identities.get(key)
        if end_user_id is None:
            end_user_id = uuid4()
            self.identities[key] = end_user_id
        erased_at = NOW if end_user_id in self.erased else None
        return UpsertedIdentity(end_user_id=end_user_id, erased_at=erased_at)

    async def create_session(
        self,
        end_user_id: UUID,
        workspace_id: UUID,
        token_digest: str,
        expires_at: datetime,
        agents: Sequence[str],
    ) -> None:
        self.sessions[token_digest] = StoredEndUserSession(
            end_user_id, workspace_id, tuple(agents)
        )

    async def find_session(
        self, token_digest: str, now: datetime
    ) -> StoredEndUserSession | None:
        del now
        if token_digest in self.revoked:
            return None
        return self.sessions.get(token_digest)

    async def revoke_sessions(self, end_user_id: UUID, workspace_id: UUID, now: datetime) -> None:
        del now
        for digest, stored in self.sessions.items():
            if stored.end_user_id == end_user_id and stored.workspace_id == workspace_id:
                self.revoked.add(digest)

    async def end_user_exists(self, workspace_id: UUID, end_user_id: UUID) -> bool:
        return any(
            eu == end_user_id and w == workspace_id for (w, _, _), eu in self.identities.items()
        )

    async def append_audit(
        self,
        *,
        workspace_id: UUID | None,
        actor_type: str,
        actor_id: UUID | None,
        action: str,
        resource_type: str,
        resource_id: UUID | None,
        request_id: str,
        context: dict[str, str],
    ) -> None:
        self.audits.append(
            {
                "workspace_id": workspace_id,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "request_id": request_id,
                "context": context,
            }
        )


class FakeKeySource:
    """Direct pass-through: JWKS resolution is a separate module's concern
    (`test_jwks_key_source.py`), not this one's."""

    async def resolve(
        self, *, public_key: str | None, jwks_url: str | None, token: str
    ) -> str | None:
        del jwks_url, token
        return public_key


def service(
    store: FakeEndUserStore | None = None,
) -> tuple[EndUserIdentityService, FakeEndUserStore]:
    backing = store or FakeEndUserStore()
    return (
        EndUserIdentityService(backing, FakeKeySource(), session_ttl=timedelta(hours=8)),
        backing,
    )


# -- a full exchange -----------------------------------------------------


async def test_a_verified_credential_exchanges_for_a_session() -> None:
    store = FakeEndUserStore()
    store.register()
    svc, _ = service(store)
    token = rs256(claims())

    result = await svc.exchange(token, WORKSPACE_ID, NOW, "req-1")

    assert isinstance(result.end_user_id, UUID)
    assert result.session_token
    assert result.expires_at == NOW + timedelta(hours=8)


async def test_exchanging_writes_an_audit_line_naming_both_the_session_and_the_channel() -> None:
    store = FakeEndUserStore()
    store.register()
    svc, _ = service(store)
    token = rs256(claims())

    result = await svc.exchange(token, WORKSPACE_ID, NOW, "req-1")

    assert len(store.audits) == 1
    audit = store.audits[0]
    context = audit["context"]
    assert audit["action"] == "end_user.session_exchanged"
    assert audit["resource_id"] == result.end_user_id
    assert isinstance(context, dict)
    assert context["channel"] == CHANNEL


# -- idempotent upsert, §282 --------------------------------------------


async def test_the_same_sub_exchanging_twice_gets_the_same_end_user_id() -> None:
    store = FakeEndUserStore()
    store.register()
    svc, _ = service(store)

    first = await svc.exchange(rs256(claims()), WORKSPACE_ID, NOW, "req-1")
    second = await svc.exchange(rs256(claims()), WORKSPACE_ID, NOW, "req-2")

    assert first.end_user_id == second.end_user_id
    assert first.session_token != second.session_token


async def test_a_different_sub_gets_a_different_end_user_id() -> None:
    store = FakeEndUserStore()
    store.register()
    svc, _ = service(store)

    first = await svc.exchange(rs256(claims()), WORKSPACE_ID, NOW, "req-1")
    second = await svc.exchange(
        rs256(claims(sub="acme-user-99")), WORKSPACE_ID, NOW, "req-2"
    )

    assert first.end_user_id != second.end_user_id


# -- refusals, §8 ---------------------------------------------------------


async def test_a_credential_from_a_disabled_issuer_is_refused() -> None:
    store = FakeEndUserStore()
    store.register(status=ChannelIssuerStatus.DISABLED)
    svc, _ = service(store)

    with pytest.raises(CredentialExchangeRefused) as excinfo:
        await svc.exchange(rs256(claims()), WORKSPACE_ID, NOW, "req-1")

    assert excinfo.value.reason == RefusalReason.INVALID


async def test_a_bad_signature_is_refused() -> None:
    store = FakeEndUserStore()
    store.register()
    svc, _ = service(store)
    token = rs256(claims(), key=_OTHER_PRIVATE_PEM)

    with pytest.raises(CredentialExchangeRefused) as excinfo:
        await svc.exchange(token, WORKSPACE_ID, NOW, "req-1")

    assert excinfo.value.reason == RefusalReason.INVALID


async def test_an_expired_ceiling_is_refused_with_the_distinguishable_reason() -> None:
    store = FakeEndUserStore()
    store.register()
    svc, _ = service(store)
    token = rs256(claims(exp=int((NOW + timedelta(hours=24)).timestamp())))

    with pytest.raises(CredentialExchangeRefused) as excinfo:
        await svc.exchange(token, WORKSPACE_ID, NOW, "req-1")

    assert excinfo.value.reason == RefusalReason.LIFETIME_EXCEEDS_PLATFORM_CEILING


async def test_an_erased_subjects_credential_cannot_exchange_for_a_session() -> None:
    store = FakeEndUserStore()
    store.register()
    svc, _ = service(store)
    first = await svc.exchange(rs256(claims()), WORKSPACE_ID, NOW, "req-1")
    store.erased.add(first.end_user_id)

    with pytest.raises(CredentialExchangeRefused) as excinfo:
        await svc.exchange(rs256(claims()), WORKSPACE_ID, NOW, "req-2")

    assert excinfo.value.reason == RefusalReason.INVALID


async def test_an_unregistered_issuer_is_refused_the_same_way_as_a_disabled_one() -> None:
    store = FakeEndUserStore()  # nothing registered at all
    svc, _ = service(store)

    with pytest.raises(CredentialExchangeRefused) as excinfo:
        await svc.exchange(rs256(claims()), WORKSPACE_ID, NOW, "req-1")

    assert excinfo.value.reason == RefusalReason.INVALID


async def test_an_unregistered_issuer_still_pays_for_a_real_signature_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finding this task closes: a genuinely unregistered issuer has no
    `channel_issuers` row at all, so a lookup miss could answer for free —
    exactly the timing gap §8 wants shut. Proved the way the credential
    verifier's own equivalent test proves it: a spy on `jwt.decode` shows the
    expensive path ran, not a stopwatch."""
    store = FakeEndUserStore()  # nothing registered
    svc, _ = service(store)
    token = rs256(claims())
    calls: list[str] = []
    real_decode = jwt.decode

    def spy_decode(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append("decoded")
        return real_decode(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "tiny_hermes.identity.domain.end_user_credential.jwt.decode", spy_decode
    )

    with pytest.raises(CredentialExchangeRefused):
        await svc.exchange(token, WORKSPACE_ID, NOW, "req-1")

    assert calls == ["decoded"]


# -- authenticate, for `resolve_end_user_caller` --------------------------


async def test_authenticate_resolves_a_stored_session() -> None:
    store = FakeEndUserStore()
    store.register()
    svc, _ = service(store)
    exchanged = await svc.exchange(rs256(claims()), WORKSPACE_ID, NOW, "req-1")

    result = await svc.authenticate(exchanged.session_token, NOW)

    assert result == EndUserSession(
        exchanged.end_user_id, WORKSPACE_ID, ("support-bot",)
    )


async def test_authenticate_rejects_an_unknown_token() -> None:
    svc, _ = service()

    result = await svc.authenticate("not-a-real-token", NOW)

    assert result is None


async def test_authenticate_rejects_a_revoked_session() -> None:
    store = FakeEndUserStore()
    store.register()
    svc, _ = service(store)
    exchanged = await svc.exchange(rs256(claims()), WORKSPACE_ID, NOW, "req-1")
    await svc.revoke_sessions(
        _admin_actor(store), WORKSPACE_ID, exchanged.end_user_id, "req-2", NOW
    )

    result = await svc.authenticate(exchanged.session_token, NOW)

    assert result is None


# -- revocation, design §4.3 -----------------------------------------------


def _admin_actor(store: FakeEndUserStore, workspace_id: UUID = WORKSPACE_ID) -> Actor:
    actor = Actor.new(is_platform_admin=False)
    store.memberships[(workspace_id, actor.id)] = Role.WORKSPACE_ADMIN
    return actor


async def test_a_workspace_admin_can_revoke_an_end_users_sessions() -> None:
    store = FakeEndUserStore()
    store.register()
    svc, _ = service(store)
    exchanged = await svc.exchange(rs256(claims()), WORKSPACE_ID, NOW, "req-1")
    admin = _admin_actor(store)

    await svc.revoke_sessions(admin, WORKSPACE_ID, exchanged.end_user_id, "req-2", NOW)

    assert await svc.authenticate(exchanged.session_token, NOW) is None


async def test_disabling_an_issuer_does_not_revoke_an_already_exchanged_session() -> None:
    """Design §4.3's stated trade-off: disabling stops *new* credentials, not
    live sessions. That is the one asserted here — not a bug to fix."""
    store = FakeEndUserStore()
    store.register()
    svc, _ = service(store)
    exchanged = await svc.exchange(rs256(claims()), WORKSPACE_ID, NOW, "req-1")

    for key, record in list(store.issuers.items()):
        store.issuers[key] = replace(record, status=ChannelIssuerStatus.DISABLED)

    assert await svc.authenticate(exchanged.session_token, NOW) == EndUserSession(
        exchanged.end_user_id, WORKSPACE_ID, ("support-bot",)
    )


async def test_a_non_admin_cannot_revoke_sessions() -> None:
    store = FakeEndUserStore()
    store.register()
    svc, _ = service(store)
    exchanged = await svc.exchange(rs256(claims()), WORKSPACE_ID, NOW, "req-1")
    viewer = Actor.new(is_platform_admin=False)
    store.memberships[(WORKSPACE_ID, viewer.id)] = Role.VIEWER

    with pytest.raises(ForbiddenEndUserAction):
        await svc.revoke_sessions(viewer, WORKSPACE_ID, exchanged.end_user_id, "req-2", NOW)


# -- issuer registry, design §3 --------------------------------------------


async def test_a_workspace_admin_registers_an_issuer_and_it_is_audited() -> None:
    store = FakeEndUserStore()
    svc, _ = service(store)
    admin = _admin_actor(store)

    record = await svc.register_issuer(
        admin,
        WORKSPACE_ID,
        channel=CHANNEL,
        issuer=ISSUER,
        public_key=RSA_PUBLIC_PEM,
        jwks_url=None,
        allowed_origins=["https://acme.example"],
        request_id="req-1",
    )

    assert record.channel == CHANNEL
    assert record.issuer == ISSUER
    assert any(a["action"] == "end_user.issuer_registered" for a in store.audits)


async def test_a_non_admin_cannot_register_an_issuer() -> None:
    store = FakeEndUserStore()
    svc, _ = service(store)
    viewer = Actor.new(is_platform_admin=False)
    store.memberships[(WORKSPACE_ID, viewer.id)] = Role.VIEWER

    with pytest.raises(ForbiddenEndUserAction):
        await svc.register_issuer(
            viewer,
            WORKSPACE_ID,
            channel=CHANNEL,
            issuer=ISSUER,
            public_key=RSA_PUBLIC_PEM,
            jwks_url=None,
            allowed_origins=[],
            request_id="req-1",
        )


async def test_an_issuer_with_neither_a_public_key_nor_a_jwks_url_is_invalid() -> None:
    store = FakeEndUserStore()
    svc, _ = service(store)
    admin = _admin_actor(store)

    with pytest.raises(InvalidChannelIssuer):
        await svc.register_issuer(
            admin,
            WORKSPACE_ID,
            channel=CHANNEL,
            issuer=ISSUER,
            public_key=None,
            jwks_url=None,
            allowed_origins=[],
            request_id="req-1",
        )


async def test_an_issuer_with_both_a_public_key_and_a_jwks_url_is_invalid() -> None:
    store = FakeEndUserStore()
    svc, _ = service(store)
    admin = _admin_actor(store)

    with pytest.raises(InvalidChannelIssuer):
        await svc.register_issuer(
            admin,
            WORKSPACE_ID,
            channel=CHANNEL,
            issuer=ISSUER,
            public_key=RSA_PUBLIC_PEM,
            jwks_url="https://idp.acme.example/jwks.json",
            allowed_origins=[],
            request_id="req-1",
        )


async def test_any_workspace_member_can_list_issuers_but_a_stranger_cannot() -> None:
    store = FakeEndUserStore()
    store.register()
    svc, _ = service(store)
    viewer = Actor.new(is_platform_admin=False)
    store.memberships[(WORKSPACE_ID, viewer.id)] = Role.VIEWER
    stranger = Actor.new(is_platform_admin=False)

    listed = await svc.list_issuers(viewer, WORKSPACE_ID)
    assert len(listed) == 1

    with pytest.raises(ForbiddenEndUserAction):
        await svc.list_issuers(stranger, WORKSPACE_ID)


async def test_disabling_an_unknown_issuer_is_named_rather_than_silent() -> None:
    store = FakeEndUserStore()
    svc, _ = service(store)
    admin = _admin_actor(store)

    with pytest.raises(UnknownChannelIssuer):
        await svc.disable_issuer(admin, WORKSPACE_ID, uuid4(), "req-1")


async def test_disabling_an_issuer_is_audited() -> None:
    store = FakeEndUserStore()
    registered = store.register()
    svc, _ = service(store)
    admin = _admin_actor(store)

    record = await svc.disable_issuer(admin, WORKSPACE_ID, registered.id, "req-1")

    assert record.status == ChannelIssuerStatus.DISABLED
    assert any(a["action"] == "end_user.issuer_disabled" for a in store.audits)
