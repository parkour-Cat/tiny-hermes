"""Who may register somebody else's API here, and what a registration must be.

The shape is `SkillCatalog`'s — the same roles, the same audit lines, the same
"a version is immutable and the same content is the same version". A second
vocabulary for one idea would be a second place to get it wrong.

What is new is one check the skill catalog does not need: a tool is refused
unless its host is already inside the workspace's approved outbound scope. That
is M2C-1's first real consumer, and it is checked here rather than at the call
because a tool nobody may reach is a tool that will fail in the middle of
somebody's Run for a reason that has nothing to do with their Run.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from tiny_hermes.http_tools.domain.models import (
    NAME_MAX_LENGTH,
    NAME_PATTERN,
    HttpTool,
    HttpToolStatus,
    HttpToolVersion,
)
from tiny_hermes.outbound.domain.scope import OutboundScope, ScopeEntryInvalid, parse_entry
from tiny_hermes.tenancy.domain.models import Actor, Role
from tiny_hermes.tools.domain.openapi import OpenApiRefused, parse_document

WRITERS = frozenset({Role.WORKSPACE_ADMIN, Role.DEVELOPER})
READERS = frozenset({Role.WORKSPACE_ADMIN, Role.DEVELOPER, Role.VIEWER})

_NAME = re.compile(NAME_PATTERN)


@dataclass(frozen=True)
class VersionResult:
    version: HttpToolVersion
    #: False when this document was already a version. The route answers 200
    #: instead of 201 — re-registering an unchanged export is not a
    #: publication.
    created: bool


class HttpToolStore(Protocol):
    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def create_tool(
        self,
        *,
        workspace_id: UUID,
        name: str,
        base_url: str,
        credential_ref: str | None,
        created_by: UUID,
    ) -> HttpTool: ...

    async def get_tool(self, tool_id: UUID) -> HttpTool | None: ...

    async def list_tools(self, workspace_id: UUID) -> Sequence[HttpTool]: ...

    async def add_version(
        self,
        *,
        http_tool_id: UUID,
        document: str,
        created_by: UUID,
    ) -> VersionResult: ...

    async def get_version(self, version_id: UUID) -> HttpToolVersion | None: ...

    async def list_versions(self, tool_id: UUID) -> Sequence[HttpToolVersion]: ...

    async def read_document(self, version_id: UUID) -> str | None: ...

    async def set_version_status(
        self, version_id: UUID, status: HttpToolStatus
    ) -> HttpToolVersion | None: ...

    async def set_current_version(
        self, tool_id: UUID, version_id: UUID | None
    ) -> HttpTool | None: ...

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


class HttpToolError(Exception):
    """Base for every expected refusal here."""


class ForbiddenHttpToolAction(HttpToolError):
    pass


class UnknownHttpTool(HttpToolError):
    pass


class UnknownHttpToolVersion(HttpToolError):
    pass


class HttpToolNameTaken(HttpToolError):
    pass


class InvalidHttpToolName(HttpToolError):
    def __init__(self, name: str) -> None:
        super().__init__(f"{name!r} is not a tool name")
        self.name = name


class InvalidOpenApiDocument(HttpToolError):
    """The document is not one this platform can call. Carries the reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidBaseUrl(HttpToolError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HostOutsideWorkspaceScope(HttpToolError):
    """A tool whose host this workspace may not reach.

    Refused at registration rather than at the call: the alternative is a tool
    that sits in a list looking usable and fails in the middle of somebody's
    Run for a reason that has nothing to do with their Run.
    """

    def __init__(self, host: str) -> None:
        super().__init__(f"{host} is outside this workspace's outbound scope")
        self.host = host


class HttpToolCatalog:
    def __init__(self, store: HttpToolStore, scopes: WorkspaceScopeReader | None = None) -> None:
        self._store = store
        # Optional for the reason `AgentCatalog`'s readers are: the fast domain
        # tests are about who may act, not about configuration. Absent, a
        # registration is refused rather than allowed — a platform that cannot
        # check what it may reach must not register a target.
        self._scopes = scopes

    async def list_tools(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> Sequence[HttpTool]:
        await self._require_reader(actor, workspace_id, request_id)
        return await self._store.list_tools(workspace_id)

    async def list_versions(
        self, actor: Actor, workspace_id: UUID, tool_id: UUID, request_id: str
    ) -> Sequence[HttpToolVersion]:
        await self._require_reader(actor, workspace_id, request_id)
        tool = await self._visible(workspace_id, tool_id)
        return await self._store.list_versions(tool.id)

    async def register(
        self,
        actor: Actor,
        workspace_id: UUID,
        *,
        name: str,
        base_url: str,
        document: str,
        credential_ref: str | None,
        request_id: str,
    ) -> tuple[HttpTool, HttpToolVersion]:
        """A new tool and its first version."""
        await self._require_writer(actor, workspace_id, request_id)
        if not _NAME.match(name) or len(name) > NAME_MAX_LENGTH:
            raise InvalidHttpToolName(name)
        host = await self._checked_host(workspace_id, base_url)
        _parsed(document)
        try:
            tool = await self._store.create_tool(
                workspace_id=workspace_id,
                name=name,
                base_url=base_url,
                credential_ref=credential_ref,
                created_by=actor.id,
            )
        except ValueError as clash:
            raise HttpToolNameTaken from clash
        result = await self._store.add_version(
            http_tool_id=tool.id, document=document, created_by=actor.id
        )
        updated = await self._store.set_current_version(tool.id, result.version.id)
        if updated is None:  # pragma: no cover - written a line above
            raise UnknownHttpTool
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="http_tool.registered",
            resource_id=tool.id,
            request_id=request_id,
            context={"name": name, "host": host},
        )
        return updated, result.version

    async def add_version(
        self,
        actor: Actor,
        workspace_id: UUID,
        tool_id: UUID,
        document: str,
        request_id: str,
    ) -> VersionResult:
        """A new version of a tool that exists.

        Registering the same document again answers with the version it already
        is, and the route says 200 rather than 201: an unchanged export is not
        a publication, and a version list that grew a row every time somebody
        re-uploaded would make rollback a guessing game.
        """
        tool = await self._writable(actor, workspace_id, tool_id, request_id)
        _parsed(document)
        result = await self._store.add_version(
            http_tool_id=tool.id, document=document, created_by=actor.id
        )
        if result.created:
            await self._store.append_audit(
                workspace_id=workspace_id,
                actor_id=actor.id,
                action="http_tool.version_added",
                resource_id=result.version.id,
                request_id=request_id,
                context={"version": str(result.version.version_number)},
            )
        return result

    async def withdraw_version(
        self,
        actor: Actor,
        workspace_id: UUID,
        tool_id: UUID,
        version_id: UUID,
        request_id: str,
    ) -> HttpToolVersion:
        """Stop new bindings. Agents already bound keep working."""
        tool = await self._writable(actor, workspace_id, tool_id, request_id)
        version = await self._store.get_version(version_id)
        if version is None or version.http_tool_id != tool.id:
            raise UnknownHttpToolVersion
        withdrawn = await self._store.set_version_status(
            version.id, HttpToolStatus.WITHDRAWN
        )
        if withdrawn is None:  # pragma: no cover - read a line above
            raise UnknownHttpToolVersion
        if tool.current_version_id == withdrawn.id:
            # Otherwise the default for new bindings points at something no new
            # binding may name, and the next one fails about the wrong thing.
            await self._store.set_current_version(tool.id, None)
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="http_tool.version_withdrawn",
            resource_id=withdrawn.id,
            request_id=request_id,
        )
        return withdrawn

    async def _checked_host(self, workspace_id: UUID, base_url: str) -> str:
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise InvalidBaseUrl(
                f"{base_url!r} is not an http or https URL with a host"
            )
        if parts.query or parts.fragment:
            raise InvalidBaseUrl("a base URL carries no query and no fragment")
        host = parts.hostname
        if self._scopes is None:
            raise HostOutsideWorkspaceScope(host)
        approved = await self._scopes.workspace(workspace_id)
        try:
            wanted = parse_entry(host)
        except ScopeEntryInvalid as refused:
            raise InvalidBaseUrl(str(refused)) from refused
        if not any(entry.contains(wanted) for entry in approved.entries):
            raise HostOutsideWorkspaceScope(host)
        return host

    async def _visible(self, workspace_id: UUID, tool_id: UUID) -> HttpTool:
        tool = await self._store.get_tool(tool_id)
        if tool is None or tool.workspace_id != workspace_id:
            # Another workspace's tool is not found here, the answer
            # `SkillCatalog._visible` gives and for the same reason.
            raise UnknownHttpTool
        return tool

    async def _writable(
        self, actor: Actor, workspace_id: UUID, tool_id: UUID, request_id: str
    ) -> HttpTool:
        tool = await self._visible(workspace_id, tool_id)
        await self._require_writer(actor, workspace_id, request_id)
        return tool

    async def _require_writer(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> None:
        if actor.is_service_account:
            # Registering an API this platform will call is a decision a person
            # makes. A key that could add one would be a quiet way to widen
            # what every Agent in the workspace can do.
            raise ForbiddenHttpToolAction
        await self._require_role(actor, workspace_id, request_id, allowed=WRITERS)

    async def _require_reader(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> None:
        if actor.is_service_account:
            if actor.role is None or actor.role not in READERS:
                raise ForbiddenHttpToolAction
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
                raise ForbiddenHttpToolAction
            return
        if not actor.is_platform_admin:
            raise ForbiddenHttpToolAction
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="http_tool.write_by_platform_admin",
            resource_id=workspace_id,
            request_id=request_id,
        )


def _parsed(document: str) -> None:
    """Refuse a document that is not one this platform can call.

    Parsed here as well as in the store so nothing is written that cannot be
    read back into operations — a row whose document does not parse is a tool
    that exists and can never be bound.
    """
    try:
        parse_document(document)
    except OpenApiRefused as refused:
        raise InvalidOpenApiDocument(str(refused)) from refused
