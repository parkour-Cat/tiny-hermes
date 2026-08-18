"""Who may approve an outbound target, and what an approval has to be.

Product design §16.5: 平台管理员可以显式批准企业私有端点或网段；工作空间管理员
只能在已批准范围内选择目标，不能自行打开内网. Those are two different powers and
this module is the difference between them.

The shape is `SkillCatalog`'s, deliberately: the same roles, the same audit
lines, the same "a platform write needs platform authority" check. A second
vocabulary for the same idea would be a second place to get it wrong.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.outbound.domain.scope import (
    OutboundScope,
    ScopeEntryInvalid,
    parse_entry,
)
from tiny_hermes.tenancy.domain.models import Actor, Role

WRITERS = frozenset({Role.WORKSPACE_ADMIN})
READERS = frozenset({Role.WORKSPACE_ADMIN, Role.DEVELOPER, Role.VIEWER})


@dataclass(frozen=True)
class ScopeEntryRecord:
    """One approved entry as it is stored and shown."""

    id: UUID
    level: str
    workspace_id: UUID | None
    entry: str
    note: str | None
    created_by: UUID
    created_at: datetime
    #: Set when this entry exists because a model endpoint was registered. Such
    #: an entry is not editable by hand: it appears and disappears with the
    #: endpoint, so that the two can never disagree.
    endpoint_id: UUID | None = None

    @property
    def managed(self) -> bool:
        return self.endpoint_id is not None


class OutboundScopeError(Exception):
    """Base for every expected refusal here."""


class ForbiddenScopeAction(OutboundScopeError):
    pass


class InvalidScopeEntry(OutboundScopeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ScopeEntryNotFound(OutboundScopeError):
    pass


class ScopeEntryManaged(OutboundScopeError):
    """Asked to hand-edit an entry a model endpoint owns."""


class ScopeEntryOutsidePlatform(OutboundScopeError):
    """A workspace naming something the platform never approved.

    Refused at the moment of writing rather than left to fail at connection
    time: an entry that can never match is a line in a list that reads like a
    permission and is not one.
    """

    def __init__(self, entry: str) -> None:
        super().__init__(f"{entry!r} is outside the platform's approved range")
        self.entry = entry


class ScopeStore(Protocol):
    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def list_entries(
        self, level: str, workspace_id: UUID | None
    ) -> Sequence[ScopeEntryRecord]: ...

    async def add_entry(
        self,
        *,
        level: str,
        workspace_id: UUID | None,
        entry: str,
        note: str | None,
        created_by: UUID,
        endpoint_id: UUID | None = None,
    ) -> ScopeEntryRecord: ...

    async def remove_entry(self, entry_id: UUID) -> ScopeEntryRecord | None: ...

    async def get_entry(self, entry_id: UUID) -> ScopeEntryRecord | None: ...

    async def remove_endpoint_entries(self, endpoint_id: UUID) -> int: ...

    async def append_audit(
        self,
        *,
        workspace_id: UUID | None,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None: ...


class OutboundScopes:
    """The two levels, and the rule that one may not exceed the other."""

    def __init__(self, store: ScopeStore) -> None:
        self._store = store

    async def platform(self) -> OutboundScope:
        entries = await self._store.list_entries("platform", None)
        return OutboundScope.of(record.entry for record in entries)

    async def workspace(self, workspace_id: UUID) -> OutboundScope:
        entries = await self._store.list_entries("workspace", workspace_id)
        return OutboundScope.of(record.entry for record in entries)

    async def list_platform(
        self, actor: Actor, request_id: str
    ) -> Sequence[ScopeEntryRecord]:
        """Readable by any signed-in administrator of any workspace.

        What the platform approved is what every workspace is measured against,
        so a workspace administrator choosing their own entries has to be able
        to see the range they are choosing inside.
        """
        del request_id
        if actor.is_service_account:
            raise ForbiddenScopeAction
        return await self._store.list_entries("platform", None)

    async def list_workspace(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> Sequence[ScopeEntryRecord]:
        await self._require_reader(actor, workspace_id, request_id)
        return await self._store.list_entries("workspace", workspace_id)

    async def approve_platform(
        self, actor: Actor, entry: str, note: str | None, request_id: str
    ) -> ScopeEntryRecord:
        if actor.is_service_account or not actor.is_platform_admin:
            raise ForbiddenScopeAction
        parsed = _parsed(entry)
        record = await self._store.add_entry(
            level="platform",
            workspace_id=None,
            entry=parsed,
            note=note,
            created_by=actor.id,
        )
        await self._store.append_audit(
            workspace_id=None,
            actor_id=actor.id,
            action="outbound.platform_entry_approved",
            resource_id=record.id,
            request_id=request_id,
            context={"entry": parsed},
        )
        return record

    async def approve_workspace(
        self,
        actor: Actor,
        workspace_id: UUID,
        entry: str,
        note: str | None,
        request_id: str,
    ) -> ScopeEntryRecord:
        """A workspace choosing inside what the platform already approved.

        The containment check is here rather than at connection time because
        an entry that could never match is worse than a refusal: it sits in a
        list looking like a permission somebody has.
        """
        await self._require_admin(actor, workspace_id, request_id)
        parsed = _parsed(entry)
        platform = await self.platform()
        if not _covered(platform, parsed):
            raise ScopeEntryOutsidePlatform(parsed)
        record = await self._store.add_entry(
            level="workspace",
            workspace_id=workspace_id,
            entry=parsed,
            note=note,
            created_by=actor.id,
        )
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="outbound.workspace_entry_approved",
            resource_id=record.id,
            request_id=request_id,
            context={"entry": parsed},
        )
        return record

    async def revoke(
        self, actor: Actor, entry_id: UUID, request_id: str, workspace_id: UUID | None
    ) -> ScopeEntryRecord:
        record = await self._store.get_entry(entry_id)
        if record is None:
            raise ScopeEntryNotFound
        if record.level == "platform":
            if actor.is_service_account or not actor.is_platform_admin:
                raise ForbiddenScopeAction
        elif record.workspace_id is None:  # pragma: no cover - the CHECK forbids it
            raise ScopeEntryNotFound
        else:
            if record.workspace_id != workspace_id:
                # Another workspace's entry is not found here, the same answer
                # `SkillCatalog._visible` gives and for the same reason.
                raise ScopeEntryNotFound
            await self._require_admin(actor, record.workspace_id, request_id)
        if record.managed:
            # Removing it by hand would make the endpoint unreachable with
            # nothing saying why, and the next endpoint write would put it back.
            raise ScopeEntryManaged
        removed = await self._store.remove_entry(entry_id)
        if removed is None:  # pragma: no cover - read a line above
            raise ScopeEntryNotFound
        await self._store.append_audit(
            workspace_id=record.workspace_id,
            actor_id=actor.id,
            action="outbound.entry_revoked",
            resource_id=entry_id,
            request_id=request_id,
            context={"entry": record.entry, "level": record.level},
        )
        return removed

    async def approve_endpoint_host(
        self, *, endpoint_id: UUID, host: str, created_by: UUID
    ) -> None:
        """Registering a model endpoint approves the host it names.

        Not a second decision: choosing an endpoint *is* a platform
        administrator approving that address, and requiring them to write it
        down again would add a step that is forgotten rather than a judgement
        that is made. The entry says which endpoint owns it, so nobody edits it
        by hand and disabling the endpoint takes it away again.
        """
        try:
            parsed = parse_entry(host)
        except ScopeEntryInvalid:
            # An endpoint whose host is not a plain name — an address literal,
            # say — approves nothing here. The address policy still governs it,
            # and a platform administrator can approve the range by hand.
            return
        await self._store.add_entry(
            level="platform",
            workspace_id=None,
            entry=parsed.text,
            note="model endpoint",
            created_by=created_by,
            endpoint_id=endpoint_id,
        )

    async def withdraw_endpoint_host(self, endpoint_id: UUID) -> int:
        return await self._store.remove_endpoint_entries(endpoint_id)

    async def _require_admin(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> None:
        if actor.is_service_account:
            # Outbound scope is a governance decision. A key that could widen
            # it would be a way around the person who is accountable for it.
            raise ForbiddenScopeAction
        role = await self._store.user_role(workspace_id, actor.id)
        if role is not None:
            if role not in WRITERS:
                raise ForbiddenScopeAction
            return
        if not actor.is_platform_admin:
            raise ForbiddenScopeAction
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="outbound.write_by_platform_admin",
            resource_id=workspace_id,
            request_id=request_id,
        )

    async def _require_reader(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> None:
        if actor.is_service_account:
            if actor.role is None or actor.role not in READERS:
                raise ForbiddenScopeAction
            return
        role = await self._store.user_role(workspace_id, actor.id)
        if role is not None:
            if role not in READERS:
                raise ForbiddenScopeAction
            return
        if not actor.is_platform_admin:
            raise ForbiddenScopeAction
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="outbound.read_by_platform_admin",
            resource_id=workspace_id,
            request_id=request_id,
        )


def _parsed(entry: str) -> str:
    try:
        return parse_entry(entry).text
    except ScopeEntryInvalid as refused:
        raise InvalidScopeEntry(str(refused)) from refused


def _covered(platform: OutboundScope, entry: str) -> bool:
    """Whether the platform's range already contains this one.

    Uses the same containment the intersection uses, so what an administrator
    is allowed to write and what a connection is allowed to reach cannot drift
    apart.
    """
    wanted = parse_entry(entry)
    return any(approved.contains(wanted) for approved in platform.entries)
