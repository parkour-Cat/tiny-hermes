"""Who may register somebody else's MCP server here, and what happens then.

The rules are `HttpToolCatalog`'s, restated as little as possible: the same
roles, the same audit lines, the same "a version is immutable and the same
content is the same version", the same refusal for a host outside the
workspace's outbound scope.

One thing is genuinely different and it is the reason this service exists
rather than a shared one. A registration here does not accept a document — it
*asks the server* what it can do, through the platform's outbound face and
therefore across the egress boundary. So a server nobody can reach cannot be
registered, which is the honest failure: a row that looks usable and answers
nothing is worse than no row.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from tiny_hermes.mcp.domain.models import (
    NAME_MAX_LENGTH,
    NAME_PATTERN,
    McpServer,
    McpServerStatus,
    McpServerVersion,
)
from tiny_hermes.outbound.domain.scope import (
    OutboundScope,
    ScopeEntryInvalid,
    parse_entry,
)
from tiny_hermes.tenancy.domain.models import Actor, Role
from tiny_hermes.tools.domain.mcp import McpCapabilities, McpRefused

WRITERS = frozenset({Role.WORKSPACE_ADMIN, Role.DEVELOPER})
READERS = frozenset({Role.WORKSPACE_ADMIN, Role.DEVELOPER, Role.VIEWER})

_NAME = re.compile(NAME_PATTERN)


@dataclass(frozen=True)
class VersionResult:
    version: McpServerVersion
    #: False when this snapshot was already a version. Re-reading an unchanged
    #: server is not a publication, and a version list that grew a row every
    #: time somebody clicked refresh would make rollback a guessing game.
    created: bool


class CapabilityReader(Protocol):
    async def read(self, url: str, credential_ref: str | None) -> McpCapabilities:
        """Ask the server what it can do, across the outbound boundary.

        Raises `McpUnreachable` when the platform could not get an answer, and
        `McpRefused` when it got one it will not turn into tools.
        """
        ...


class McpStore(Protocol):
    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def create_server(
        self,
        *,
        workspace_id: UUID,
        name: str,
        url: str,
        credential_ref: str | None,
        created_by: UUID,
    ) -> McpServer: ...

    async def get_server(self, server_id: UUID) -> McpServer | None: ...

    async def list_servers(self, workspace_id: UUID) -> Sequence[McpServer]: ...

    async def add_version(
        self,
        *,
        mcp_server_id: UUID,
        capabilities: McpCapabilities,
        created_by: UUID,
    ) -> VersionResult: ...

    async def get_version(self, version_id: UUID) -> McpServerVersion | None: ...

    async def list_versions(self, server_id: UUID) -> Sequence[McpServerVersion]: ...

    async def set_version_status(
        self, version_id: UUID, status: McpServerStatus
    ) -> McpServerVersion | None: ...

    async def set_current_version(
        self, server_id: UUID, version_id: UUID | None
    ) -> McpServer | None: ...

    async def mark_validated(self, server_id: UUID, moment: datetime) -> None: ...

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None: ...


class WorkspaceScopeReader(Protocol):
    async def workspace(self, workspace_id: UUID) -> OutboundScope: ...


class McpError(Exception):
    """Base for every expected refusal here."""


class ForbiddenMcpAction(McpError):
    pass


class UnknownMcpServer(McpError):
    pass


class UnknownMcpServerVersion(McpError):
    pass


class McpServerNameTaken(McpError):
    pass


class InvalidMcpServerName(McpError):
    def __init__(self, name: str) -> None:
        super().__init__(f"{name!r} is not a server name")
        self.name = name


class InvalidMcpUrl(McpError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class McpUnreachable(McpError):
    """The platform could not get an answer out of the server.

    Not the same as a bad answer. This one may be worth trying again; a
    `McpCapabilitiesRefused` will not change on a retry.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class McpCapabilitiesRefused(McpError):
    """The server answered something this platform will not turn into tools."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HostOutsideWorkspaceScope(McpError):
    def __init__(self, host: str) -> None:
        super().__init__(f"{host} is outside this workspace's outbound scope")
        self.host = host


class McpCatalog:
    def __init__(
        self,
        store: McpStore,
        reader: CapabilityReader | None = None,
        scopes: WorkspaceScopeReader | None = None,
    ) -> None:
        self._store = store
        # Both optional for the reason `HttpToolCatalog`'s are, and with the
        # same consequence: absent, a registration is refused rather than
        # allowed. A platform that cannot check what it may reach, or cannot
        # reach it, must not record a server as usable.
        self._reader = reader
        self._scopes = scopes

    async def list_servers(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> Sequence[McpServer]:
        await self._require_reader(actor, workspace_id, request_id)
        return await self._store.list_servers(workspace_id)

    async def list_versions(
        self, actor: Actor, workspace_id: UUID, server_id: UUID, request_id: str
    ) -> Sequence[McpServerVersion]:
        await self._require_reader(actor, workspace_id, request_id)
        server = await self._visible(workspace_id, server_id)
        return await self._store.list_versions(server.id)

    async def register(
        self,
        actor: Actor,
        workspace_id: UUID,
        *,
        name: str,
        url: str,
        credential_ref: str | None,
        request_id: str,
    ) -> tuple[McpServer, McpServerVersion]:
        """A new server and the first snapshot of what it advertises."""
        await self._require_writer(actor, workspace_id, request_id)
        if not _NAME.match(name) or len(name) > NAME_MAX_LENGTH:
            raise InvalidMcpServerName(name)
        host = await self._checked_host(workspace_id, url)
        capabilities = await self._read(url, credential_ref)
        try:
            server = await self._store.create_server(
                workspace_id=workspace_id,
                name=name,
                url=url,
                credential_ref=credential_ref,
                created_by=actor.id,
            )
        except ValueError as clash:
            raise McpServerNameTaken from clash
        result = await self._store.add_version(
            mcp_server_id=server.id, capabilities=capabilities, created_by=actor.id
        )
        updated = await self._store.set_current_version(server.id, result.version.id)
        if updated is None:  # pragma: no cover - written a line above
            raise UnknownMcpServer
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="mcp_server.registered",
            resource_id=server.id,
            request_id=request_id,
            context={"name": name, "host": host, "tools": str(len(capabilities.tools))},
        )
        return updated, result.version

    async def refresh(
        self, actor: Actor, workspace_id: UUID, server_id: UUID, request_id: str
    ) -> VersionResult:
        """Read the server again, and record a version if it has changed.

        An unchanged server answers 200 rather than 201 and adds no row: the
        point of a version is that somebody reviewed it, and a snapshot
        identical to the last one has nothing new to review.
        """
        server = await self._writable(actor, workspace_id, server_id, request_id)
        capabilities = await self._read(server.url, server.credential_ref)
        result = await self._store.add_version(
            mcp_server_id=server.id, capabilities=capabilities, created_by=actor.id
        )
        if result.created:
            await self._store.append_audit(
                workspace_id=workspace_id,
                actor_id=actor.id,
                action="mcp_server.version_added",
                resource_id=result.version.id,
                request_id=request_id,
                context={"version": str(result.version.version_number)},
            )
        return result

    async def withdraw_version(
        self,
        actor: Actor,
        workspace_id: UUID,
        server_id: UUID,
        version_id: UUID,
        request_id: str,
    ) -> McpServerVersion:
        """Stop new bindings. Agents already bound keep working."""
        server = await self._writable(actor, workspace_id, server_id, request_id)
        version = await self._store.get_version(version_id)
        if version is None or version.mcp_server_id != server.id:
            raise UnknownMcpServerVersion
        withdrawn = await self._store.set_version_status(
            version.id, McpServerStatus.WITHDRAWN
        )
        if withdrawn is None:  # pragma: no cover - read a line above
            raise UnknownMcpServerVersion
        if server.current_version_id == withdrawn.id:
            await self._store.set_current_version(server.id, None)
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="mcp_server.version_withdrawn",
            resource_id=withdrawn.id,
            request_id=request_id,
        )
        return withdrawn

    async def _read(self, url: str, credential_ref: str | None) -> McpCapabilities:
        if self._reader is None:
            raise McpUnreachable("no outbound face is configured here")
        try:
            capabilities = await self._reader.read(url, credential_ref)
        except McpRefused as refused:
            raise McpCapabilitiesRefused(str(refused)) from refused
        return capabilities

    async def _checked_host(self, workspace_id: UUID, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise InvalidMcpUrl(f"{url!r} is not an http or https URL with a host")
        if parts.fragment:
            raise InvalidMcpUrl("a server URL carries no fragment")
        host = parts.hostname
        if self._scopes is None:
            raise HostOutsideWorkspaceScope(host)
        approved = await self._scopes.workspace(workspace_id)
        try:
            wanted = parse_entry(host)
        except ScopeEntryInvalid as refused:
            raise InvalidMcpUrl(str(refused)) from refused
        if not any(entry.contains(wanted) for entry in approved.entries):
            raise HostOutsideWorkspaceScope(host)
        return host

    async def _visible(self, workspace_id: UUID, server_id: UUID) -> McpServer:
        server = await self._store.get_server(server_id)
        if server is None or server.workspace_id != workspace_id:
            raise UnknownMcpServer
        return server

    async def _writable(
        self, actor: Actor, workspace_id: UUID, server_id: UUID, request_id: str
    ) -> McpServer:
        server = await self._visible(workspace_id, server_id)
        await self._require_writer(actor, workspace_id, request_id)
        return server

    async def _require_writer(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> None:
        if actor.is_service_account:
            # Registering a server this platform will call is a decision a
            # person makes, for the reason an HTTP tool's registration is.
            raise ForbiddenMcpAction
        await self._require_role(actor, workspace_id, request_id, allowed=WRITERS)

    async def _require_reader(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> None:
        if actor.is_service_account:
            if actor.role is None or actor.role not in READERS:
                raise ForbiddenMcpAction
            return
        await self._require_role(actor, workspace_id, request_id, allowed=READERS)

    async def _require_role(
        self,
        actor: Actor,
        workspace_id: UUID,
        request_id: str,
        *,
        allowed: frozenset[Role],
    ) -> None:
        role = await self._store.user_role(workspace_id, actor.id)
        if role is not None:
            if role not in allowed:
                raise ForbiddenMcpAction
            return
        if not actor.is_platform_admin:
            raise ForbiddenMcpAction
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="mcp_server.write_by_platform_admin",
            resource_id=workspace_id,
            request_id=request_id,
        )
