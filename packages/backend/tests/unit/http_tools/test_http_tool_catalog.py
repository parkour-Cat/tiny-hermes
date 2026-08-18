"""Who may register somebody else's API here, and what a registration must be.

The rules are the skill catalog's, so most of what is pinned here is that they
really are the same rules rather than a second set that looks like them: only a
person may register, a version is immutable, and re-registering an unchanged
document is not a publication.

One rule has no counterpart there and is the reason this file exists. A tool is
refused unless its host is already inside the workspace's approved outbound
scope — checked at registration, because a tool nobody may reach is one that
sits in a list looking usable and fails in the middle of somebody's Run for a
reason that has nothing to do with their Run.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tiny_hermes.http_tools.application.service import (
    ForbiddenHttpToolAction,
    HostOutsideWorkspaceScope,
    HttpToolCatalog,
    HttpToolNameTaken,
    InvalidBaseUrl,
    InvalidHttpToolName,
    InvalidOpenApiDocument,
    UnknownHttpTool,
    UnknownHttpToolVersion,
    VersionResult,
)
from tiny_hermes.http_tools.domain.models import (
    HttpTool,
    HttpToolStatus,
    HttpToolVersion,
)
from tiny_hermes.outbound.domain.scope import OutboundScope
from tiny_hermes.tenancy.domain.models import Actor, Role
from tiny_hermes.tools.domain.openapi import parse_document

DOCUMENT = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Orders", "version": "1"},
        "paths": {
            "/orders": {
                "get": {"operationId": "listOrders", "summary": "List every order."}
            }
        },
    }
)

SECOND_DOCUMENT = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "Orders", "version": "2"},
        "paths": {
            "/orders": {
                "get": {"operationId": "listOrders", "summary": "List every order."},
                "post": {"operationId": "createOrder", "summary": "Place one."},
            }
        },
    }
)


@dataclass
class MemoryStore:
    """The smallest store the catalog's rules can be read against."""

    roles: dict[tuple[UUID, UUID], Role] = field(
        default_factory=dict[tuple[UUID, UUID], Role]
    )
    tools: dict[UUID, HttpTool] = field(default_factory=dict[UUID, HttpTool])
    versions: dict[UUID, HttpToolVersion] = field(
        default_factory=dict[UUID, HttpToolVersion]
    )
    documents: dict[UUID, str] = field(default_factory=dict[UUID, str])
    audit: list[str] = field(default_factory=list[str])

    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        return self.roles.get((workspace_id, user_id))

    async def create_tool(
        self,
        *,
        workspace_id: UUID,
        name: str,
        base_url: str,
        credential_ref: str | None,
        created_by: UUID,
    ) -> HttpTool:
        if any(
            tool.workspace_id == workspace_id and tool.name == name
            for tool in self.tools.values()
        ):
            raise ValueError("taken")
        now = datetime.now(UTC)
        tool = HttpTool(
            id=uuid4(),
            workspace_id=workspace_id,
            name=name,
            base_url=base_url,
            credential_ref=credential_ref,
            current_version_id=None,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        self.tools[tool.id] = tool
        return tool

    async def get_tool(self, tool_id: UUID) -> HttpTool | None:
        return self.tools.get(tool_id)

    async def list_tools(self, workspace_id: UUID) -> Sequence[HttpTool]:
        return [
            tool for tool in self.tools.values() if tool.workspace_id == workspace_id
        ]

    async def add_version(
        self, *, http_tool_id: UUID, document: str, created_by: UUID
    ) -> VersionResult:
        parsed = parse_document(document)
        for version in self.versions.values():
            if (
                version.http_tool_id == http_tool_id
                and version.content_hash == parsed.content_hash
            ):
                return VersionResult(version=version, created=False)
        numbers = [
            item.version_number
            for item in self.versions.values()
            if item.http_tool_id == http_tool_id
        ]
        version = HttpToolVersion(
            id=uuid4(),
            http_tool_id=http_tool_id,
            version_number=max(numbers, default=0) + 1,
            content_hash=parsed.content_hash,
            title=parsed.title,
            document_version=parsed.version,
            operations=parsed.operations,
            status=HttpToolStatus.ACTIVE,
            created_by=created_by,
            created_at=datetime.now(UTC),
        )
        self.versions[version.id] = version
        self.documents[version.id] = document
        return VersionResult(version=version, created=True)

    async def get_version(self, version_id: UUID) -> HttpToolVersion | None:
        return self.versions.get(version_id)

    async def list_versions(self, tool_id: UUID) -> Sequence[HttpToolVersion]:
        return [
            version
            for version in self.versions.values()
            if version.http_tool_id == tool_id
        ]

    async def read_document(self, version_id: UUID) -> str | None:
        return self.documents.get(version_id)

    async def set_version_status(
        self, version_id: UUID, status: HttpToolStatus
    ) -> HttpToolVersion | None:
        version = self.versions.get(version_id)
        if version is None:
            return None
        updated = replace(version, status=status)
        self.versions[version_id] = updated
        return updated

    async def set_current_version(
        self, tool_id: UUID, version_id: UUID | None
    ) -> HttpTool | None:
        tool = self.tools.get(tool_id)
        if tool is None:
            return None
        updated = replace(tool, current_version_id=version_id)
        self.tools[tool_id] = updated
        return updated

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None:
        del workspace_id, actor_id, resource_id, request_id, context
        self.audit.append(action)


@dataclass
class Scopes:
    approved: OutboundScope

    async def workspace(self, workspace_id: UUID) -> OutboundScope:
        del workspace_id
        return self.approved


@dataclass
class Setup:
    catalog: HttpToolCatalog
    store: MemoryStore
    workspace_id: UUID
    actor: Actor


def setup(
    *,
    role: Role = Role.DEVELOPER,
    approved: tuple[str, ...] = ("api.example.com",),
    scopes: bool = True,
) -> Setup:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryStore()
    store.roles[(workspace_id, actor.id)] = role
    catalog = HttpToolCatalog(
        store, Scopes(OutboundScope.of(approved)) if scopes else None
    )
    return Setup(catalog, store, workspace_id, actor)


async def register(
    fixture: Setup,
    *,
    name: str = "orders",
    base_url: str = "https://api.example.com/v2",
    document: str = DOCUMENT,
    credential_ref: str | None = "ORDERS_KEY",
) -> tuple[HttpTool, HttpToolVersion]:
    return await fixture.catalog.register(
        fixture.actor,
        fixture.workspace_id,
        name=name,
        base_url=base_url,
        document=document,
        credential_ref=credential_ref,
        request_id="req-1",
    )


# -- registering -------------------------------------------------------------


async def test_a_registration_is_a_tool_and_its_first_version() -> None:
    fixture = setup()

    tool, version = await register(fixture)

    assert tool.name == "orders"
    assert tool.current_version_id == version.id
    assert [item.operation_id for item in version.operations] == ["listOrders"]
    assert version.version_number == 1


async def test_the_host_must_already_be_inside_the_workspace_scope() -> None:
    """Refused here rather than at the call: a tool nobody may reach would fail
    in the middle of somebody's Run for a reason unrelated to their Run."""
    fixture = setup(approved=("files.example.com",))

    with pytest.raises(HostOutsideWorkspaceScope) as refused:
        await register(fixture)

    assert refused.value.host == "api.example.com"


async def test_a_platform_with_no_scopes_wired_in_refuses_rather_than_allows() -> None:
    fixture = setup(scopes=False)

    with pytest.raises(HostOutsideWorkspaceScope):
        await register(fixture)


async def test_a_wildcard_the_workspace_approved_covers_the_host() -> None:
    fixture = setup(approved=("*.example.com",))

    tool, _ = await register(fixture)

    assert tool.base_url == "https://api.example.com/v2"


@pytest.mark.parametrize(
    "base_url",
    ["ftp://api.example.com", "https://api.example.com?a=1", "not-a-url"],
)
async def test_a_base_url_that_is_not_one_is_refused(base_url: str) -> None:
    fixture = setup()

    with pytest.raises(InvalidBaseUrl):
        await register(fixture, base_url=base_url)


async def test_a_document_this_platform_cannot_call_is_refused_with_its_reason() -> None:
    fixture = setup()

    with pytest.raises(InvalidOpenApiDocument) as refused:
        await register(fixture, document='{"openapi": "3.0.3"}')

    assert refused.value.reason


async def test_a_name_a_model_would_have_to_quote_is_refused() -> None:
    fixture = setup()

    with pytest.raises(InvalidHttpToolName):
        await register(fixture, name="Orders API")


async def test_two_tools_may_not_share_a_name_in_one_workspace() -> None:
    fixture = setup()
    await register(fixture)

    with pytest.raises(HttpToolNameTaken):
        await register(fixture)


async def test_a_service_account_may_not_register_one() -> None:
    """Registering an API this platform will call is a decision a person makes.
    A key that could add one would quietly widen every Agent in the workspace."""
    fixture = setup()
    machine = Actor(uuid4(), False, role=Role.DEVELOPER, is_service_account=True)

    with pytest.raises(ForbiddenHttpToolAction):
        await fixture.catalog.register(
            machine,
            fixture.workspace_id,
            name="orders",
            base_url="https://api.example.com",
            document=DOCUMENT,
            credential_ref=None,
            request_id="req-1",
        )


async def test_a_viewer_may_read_and_may_not_register() -> None:
    fixture = setup(role=Role.VIEWER)

    with pytest.raises(ForbiddenHttpToolAction):
        await register(fixture)

    assert (
        await fixture.catalog.list_tools(fixture.actor, fixture.workspace_id, "req-2")
        == []
    )


# -- versions ----------------------------------------------------------------


async def test_a_second_document_is_a_second_version() -> None:
    fixture = setup()
    tool, first = await register(fixture)

    result = await fixture.catalog.add_version(
        fixture.actor, fixture.workspace_id, tool.id, SECOND_DOCUMENT, "req-2"
    )

    assert result.created
    assert result.version.version_number == 2
    assert result.version.id != first.id


async def test_the_same_document_is_the_same_version() -> None:
    """Re-uploading an unchanged export is not a publication, and a version
    list that grew a row every time would make rollback a guessing game."""
    fixture = setup()
    tool, first = await register(fixture)

    result = await fixture.catalog.add_version(
        fixture.actor, fixture.workspace_id, tool.id, DOCUMENT, "req-2"
    )

    assert not result.created
    assert result.version.id == first.id


async def test_another_workspace_s_tool_is_not_found_rather_than_forbidden() -> None:
    fixture = setup()
    tool, _ = await register(fixture)
    other = setup()

    with pytest.raises(UnknownHttpTool):
        await other.catalog.add_version(
            other.actor, other.workspace_id, tool.id, SECOND_DOCUMENT, "req-2"
        )


async def test_withdrawing_stops_new_bindings() -> None:
    fixture = setup()
    tool, version = await register(fixture)

    withdrawn = await fixture.catalog.withdraw_version(
        fixture.actor, fixture.workspace_id, tool.id, version.id, "req-2"
    )

    assert withdrawn.status is HttpToolStatus.WITHDRAWN
    assert not withdrawn.bindable


async def test_withdrawing_the_current_version_clears_the_pointer() -> None:
    """Otherwise the default for new bindings names something no new binding
    may use, and the next one fails about the wrong thing."""
    fixture = setup()
    tool, version = await register(fixture)

    await fixture.catalog.withdraw_version(
        fixture.actor, fixture.workspace_id, tool.id, version.id, "req-2"
    )

    assert fixture.store.tools[tool.id].current_version_id is None


async def test_withdrawing_a_version_of_another_tool_is_not_found() -> None:
    fixture = setup()
    tool, _ = await register(fixture)
    _, other_version = await register(fixture, name="billing")

    with pytest.raises(UnknownHttpToolVersion):
        await fixture.catalog.withdraw_version(
            fixture.actor, fixture.workspace_id, tool.id, other_version.id, "req-2"
        )
