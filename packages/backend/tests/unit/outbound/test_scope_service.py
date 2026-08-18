"""Who may widen an outbound range, and who may only choose inside one.

Product design §16.5 gives those to two different people: 平台管理员可以显式批准
企业私有端点或网段；工作空间管理员只能在已批准范围内选择目标，不能自行打开内网.
Everything here is that sentence, including the part that is easy to lose — a
workspace entry outside the platform's range is refused *when it is written*,
not left to fail at some connection nobody is watching.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tiny_hermes.outbound.application.service import (
    ForbiddenScopeAction,
    InvalidScopeEntry,
    OutboundScopes,
    ScopeEntryManaged,
    ScopeEntryNotFound,
    ScopeEntryOutsidePlatform,
    ScopeEntryRecord,
)
from tiny_hermes.tenancy.domain.models import Actor, Role

REQUEST = "request-1"


@dataclass
class MemoryScopeStore:
    """The smallest store the rules need, and a record of what was audited."""

    memberships: dict[tuple[UUID, UUID], Role] = field(
        default_factory=dict[tuple[UUID, UUID], Role]
    )
    entries: list[ScopeEntryRecord] = field(default_factory=list[ScopeEntryRecord])
    audits: list[str] = field(default_factory=list[str])

    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        return self.memberships.get((workspace_id, user_id))

    async def list_entries(
        self, level: str, workspace_id: UUID | None
    ) -> list[ScopeEntryRecord]:
        return [
            record
            for record in self.entries
            if record.level == level and record.workspace_id == workspace_id
        ]

    async def add_entry(
        self,
        *,
        level: str,
        workspace_id: UUID | None,
        entry: str,
        note: str | None,
        created_by: UUID,
        endpoint_id: UUID | None = None,
    ) -> ScopeEntryRecord:
        for existing in self.entries:
            if (
                existing.level == level
                and existing.workspace_id == workspace_id
                and existing.entry == entry
            ):
                return existing
        record = ScopeEntryRecord(
            id=uuid4(),
            level=level,
            workspace_id=workspace_id,
            entry=entry,
            note=note,
            created_by=created_by,
            created_at=datetime.now(UTC),
            endpoint_id=endpoint_id,
        )
        self.entries.append(record)
        return record

    async def get_entry(self, entry_id: UUID) -> ScopeEntryRecord | None:
        return next((item for item in self.entries if item.id == entry_id), None)

    async def remove_entry(self, entry_id: UUID) -> ScopeEntryRecord | None:
        found = await self.get_entry(entry_id)
        if found is not None:
            self.entries.remove(found)
        return found

    async def remove_endpoint_entries(self, endpoint_id: UUID) -> int:
        doomed = [item for item in self.entries if item.endpoint_id == endpoint_id]
        for item in doomed:
            self.entries.remove(item)
        return len(doomed)

    async def append_audit(
        self,
        *,
        workspace_id: UUID | None,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None:
        del workspace_id, actor_id, resource_id, request_id, context
        self.audits.append(action)


@pytest.fixture
def store() -> MemoryScopeStore:
    return MemoryScopeStore()


@pytest.fixture
def scopes(store: MemoryScopeStore) -> OutboundScopes:
    return OutboundScopes(store)


@pytest.fixture
def workspace_id() -> UUID:
    return uuid4()


def member(store: MemoryScopeStore, workspace_id: UUID, role: Role) -> Actor:
    actor = Actor(uuid4(), is_platform_admin=False)
    store.memberships[(workspace_id, actor.id)] = role
    return actor


def platform_admin() -> Actor:
    return Actor(uuid4(), is_platform_admin=True)


# -- the platform level -----------------------------------------------------


async def test_a_platform_administrator_approves_a_target(
    scopes: OutboundScopes, store: MemoryScopeStore
) -> None:
    admin = platform_admin()

    record = await scopes.approve_platform(admin, "*.example.com", "the vendor", REQUEST)

    assert record.entry == "*.example.com"
    assert (await scopes.platform()).allows_host("api.example.com") is True
    assert "outbound.platform_entry_approved" in store.audits


async def test_a_workspace_administrator_cannot_widen_the_platform(
    scopes: OutboundScopes, store: MemoryScopeStore, workspace_id: UUID
) -> None:
    """The whole point of two levels."""
    actor = member(store, workspace_id, Role.WORKSPACE_ADMIN)

    with pytest.raises(ForbiddenScopeAction):
        await scopes.approve_platform(actor, "*.example.com", None, REQUEST)


async def test_a_service_account_may_not_widen_anything(
    scopes: OutboundScopes, store: MemoryScopeStore, workspace_id: UUID
) -> None:
    """Outbound scope is a governance decision, and a key is not a person."""
    key = Actor(
        uuid4(),
        is_platform_admin=True,
        is_service_account=True,
        role=Role.WORKSPACE_ADMIN,
    )

    with pytest.raises(ForbiddenScopeAction):
        await scopes.approve_platform(key, "*.example.com", None, REQUEST)
    with pytest.raises(ForbiddenScopeAction):
        await scopes.approve_workspace(key, workspace_id, "api.example.com", None, REQUEST)


async def test_a_platform_with_nothing_approved_approves_nothing(
    scopes: OutboundScopes,
) -> None:
    """The default, and the reason an unconfigured deployment sends nothing."""
    assert (await scopes.platform()).empty is True


@pytest.mark.parametrize("entry", ["*", "*.com", "api-*.example.com", "not a host"])
async def test_an_entry_nobody_could_review_is_refused(
    scopes: OutboundScopes, entry: str
) -> None:
    with pytest.raises(InvalidScopeEntry):
        await scopes.approve_platform(platform_admin(), entry, None, REQUEST)


# -- the workspace level ----------------------------------------------------


async def test_a_workspace_chooses_inside_what_the_platform_approved(
    scopes: OutboundScopes, store: MemoryScopeStore, workspace_id: UUID
) -> None:
    await scopes.approve_platform(platform_admin(), "*.example.com", None, REQUEST)
    actor = member(store, workspace_id, Role.WORKSPACE_ADMIN)

    record = await scopes.approve_workspace(
        actor, workspace_id, "api.example.com", None, REQUEST
    )

    assert record.entry == "api.example.com"
    assert (await scopes.workspace(workspace_id)).allows_host("api.example.com") is True


async def test_a_workspace_naming_something_outside_is_refused_when_it_is_written(
    scopes: OutboundScopes, store: MemoryScopeStore, workspace_id: UUID
) -> None:
    """Not left to fail at a connection: an entry that can never match sits in
    a list looking like a permission somebody has."""
    await scopes.approve_platform(platform_admin(), "*.example.com", None, REQUEST)
    actor = member(store, workspace_id, Role.WORKSPACE_ADMIN)

    with pytest.raises(ScopeEntryOutsidePlatform) as refused:
        await scopes.approve_workspace(
            actor, workspace_id, "payments.other.example", None, REQUEST
        )

    assert refused.value.entry == "payments.other.example"
    assert (await scopes.workspace(workspace_id)).empty is True


async def test_a_workspace_may_not_widen_a_wildcard_it_was_given(
    scopes: OutboundScopes, store: MemoryScopeStore, workspace_id: UUID
) -> None:
    """`api.example.com` is inside `*.example.com`; the reverse is not."""
    await scopes.approve_platform(platform_admin(), "api.example.com", None, REQUEST)
    actor = member(store, workspace_id, Role.WORKSPACE_ADMIN)

    with pytest.raises(ScopeEntryOutsidePlatform):
        await scopes.approve_workspace(
            actor, workspace_id, "*.example.com", None, REQUEST
        )


async def test_a_developer_may_read_the_range_and_not_change_it(
    scopes: OutboundScopes, store: MemoryScopeStore, workspace_id: UUID
) -> None:
    await scopes.approve_platform(platform_admin(), "*.example.com", None, REQUEST)
    admin = member(store, workspace_id, Role.WORKSPACE_ADMIN)
    await scopes.approve_workspace(admin, workspace_id, "api.example.com", None, REQUEST)
    developer = member(store, workspace_id, Role.DEVELOPER)

    listed = await scopes.list_workspace(developer, workspace_id, REQUEST)

    assert [record.entry for record in listed] == ["api.example.com"]
    with pytest.raises(ForbiddenScopeAction):
        await scopes.approve_workspace(
            developer, workspace_id, "docs.example.com", None, REQUEST
        )


async def test_a_workspace_administrator_can_see_what_the_platform_approved(
    scopes: OutboundScopes, store: MemoryScopeStore, workspace_id: UUID
) -> None:
    """They are choosing inside it, so they have to be able to read it."""
    await scopes.approve_platform(platform_admin(), "*.example.com", None, REQUEST)
    actor = member(store, workspace_id, Role.WORKSPACE_ADMIN)

    assert [record.entry for record in await scopes.list_platform(actor, REQUEST)] == [
        "*.example.com"
    ]


# -- taking one away --------------------------------------------------------


async def test_revoking_a_workspace_entry_needs_that_workspace(
    scopes: OutboundScopes, store: MemoryScopeStore, workspace_id: UUID
) -> None:
    await scopes.approve_platform(platform_admin(), "*.example.com", None, REQUEST)
    actor = member(store, workspace_id, Role.WORKSPACE_ADMIN)
    record = await scopes.approve_workspace(
        actor, workspace_id, "api.example.com", None, REQUEST
    )
    elsewhere = uuid4()
    stranger = member(store, elsewhere, Role.WORKSPACE_ADMIN)

    with pytest.raises(ScopeEntryNotFound):
        await scopes.revoke(stranger, record.id, REQUEST, workspace_id=elsewhere)
    revoked = await scopes.revoke(actor, record.id, REQUEST, workspace_id=workspace_id)
    assert revoked.entry == "api.example.com"
    assert (await scopes.workspace(workspace_id)).empty is True


async def test_only_a_platform_administrator_revokes_a_platform_entry(
    scopes: OutboundScopes, store: MemoryScopeStore, workspace_id: UUID
) -> None:
    record = await scopes.approve_platform(
        platform_admin(), "*.example.com", None, REQUEST
    )
    actor = member(store, workspace_id, Role.WORKSPACE_ADMIN)

    with pytest.raises(ForbiddenScopeAction):
        await scopes.revoke(actor, record.id, REQUEST, workspace_id=workspace_id)


# -- entries a model endpoint owns ------------------------------------------


async def test_registering_an_endpoint_approves_its_host_without_a_second_step(
    scopes: OutboundScopes, store: MemoryScopeStore
) -> None:
    """Choosing an endpoint *is* the approval. Asking for it twice would add a
    step that gets forgotten rather than a judgement that gets made."""
    endpoint = uuid4()

    await scopes.approve_endpoint_host(
        endpoint_id=endpoint, host="models.example.com", created_by=uuid4()
    )

    assert (await scopes.platform()).allows_host("models.example.com") is True
    assert store.entries[0].managed is True


async def test_an_endpoint_owned_entry_is_not_edited_by_hand(
    scopes: OutboundScopes, store: MemoryScopeStore
) -> None:
    """Removing it would make the endpoint unreachable with nothing saying why,
    and the next endpoint write would put it back."""
    endpoint = uuid4()
    await scopes.approve_endpoint_host(
        endpoint_id=endpoint, host="models.example.com", created_by=uuid4()
    )

    with pytest.raises(ScopeEntryManaged):
        await scopes.revoke(
            platform_admin(), store.entries[0].id, REQUEST, workspace_id=None
        )


async def test_disabling_an_endpoint_takes_its_approval_away(
    scopes: OutboundScopes,
) -> None:
    endpoint = uuid4()
    await scopes.approve_endpoint_host(
        endpoint_id=endpoint, host="models.example.com", created_by=uuid4()
    )

    removed = await scopes.withdraw_endpoint_host(endpoint)

    assert removed == 1
    assert (await scopes.platform()).empty is True


async def test_an_endpoint_named_by_address_approves_nothing_here(
    scopes: OutboundScopes,
) -> None:
    """A literal address is not a host entry. The address policy still governs
    it, and a platform administrator can approve the range deliberately."""
    await scopes.approve_endpoint_host(
        endpoint_id=uuid4(), host="10.1.2.3:8443", created_by=uuid4()
    )

    assert (await scopes.platform()).empty is True
