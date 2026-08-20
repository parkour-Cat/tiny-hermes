"""What an Agent may call on an MCP server, and what publish refuses.

The seventh widening, and the same publish checks the HTTP bindings get — with
one that is stricter and one that is looser.

Stricter: **every** MCP binding must choose a write policy, not only one that
bound something that writes. An MCP server does not say which of its tools
change something; there is no `GET` to read. The platform cannot tell, so the
safe reading is that any of them might, and §16.3's choice has to be made for
all of them or for none.

Looser: nothing here checks a schema. What a bound tool takes is revalidated
before every Run, because the server decides it and can change it — publishing
fixes the *names*, which is the part a person reviewed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from tiny_hermes.agents.application.service import (
    AgentCatalog,
    McpBindingUnavailable,
    PreauthorizationNotPermitted,
    WritePolicyNotChosen,
)
from tiny_hermes.agents.domain.models import (
    MAX_BOUND_MCP_TOOLS,
    MAX_MCP_BINDINGS,
    AgentSpec,
    AgentVersion,
    WritePolicy,
    normalize_agent_spec,
)
from tiny_hermes.agents.infrastructure.memory_store import MemoryAgentStore
from tiny_hermes.agents.ports.mcp import McpBindingView
from tiny_hermes.outbound.domain.scope import OutboundScope
from tiny_hermes.tenancy.domain.models import Actor, Role

from .test_agent_models import valid_spec
from .test_model_policy import DETERMINISTIC_HASH

# -- the widening ------------------------------------------------------------


def test_a_spec_that_binds_no_mcp_server_hashes_to_what_it_did() -> None:
    spec = AgentSpec.model_validate(valid_spec())
    document, content_hash = normalize_agent_spec(spec)

    assert content_hash == DETERMINISTIC_HASH
    assert "mcp_tools" not in document
    assert spec.mcp_tools == ()


def test_a_binding_survives_normalization() -> None:
    version_id = uuid4()
    spec = AgentSpec.model_validate(
        {
            **valid_spec(),
            "mcp_tools": [
                {
                    "mcp_server_version_id": str(version_id),
                    "tools": ["search"],
                    "write_policy": "governance",
                }
            ],
        }
    )

    document, content_hash = normalize_agent_spec(spec)

    assert document["mcp_tools"] == [
        {
            "mcp_server_version_id": str(version_id),
            "tools": ["search"],
            "write_policy": "governance",
        }
    ]
    assert content_hash != DETERMINISTIC_HASH


# -- what an author may write ------------------------------------------------


def test_there_is_no_way_to_bind_everything() -> None:
    """§16.2's rule, made unwriteable rather than merely forbidden: a field
    that could say "all" gets written as "all" on the first day."""
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                **valid_spec(),
                "mcp_tools": [{"mcp_server_version_id": str(uuid4()), "tools": "all"}],
            }
        )


def test_binding_no_tool_is_refused() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                **valid_spec(),
                "mcp_tools": [{"mcp_server_version_id": str(uuid4()), "tools": []}],
            }
        )


def test_the_same_version_twice_is_refused() -> None:
    version_id = str(uuid4())

    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                **valid_spec(),
                "mcp_tools": [
                    {"mcp_server_version_id": version_id, "tools": ["a"]},
                    {"mcp_server_version_id": version_id, "tools": ["b"]},
                ],
            }
        )


def test_one_tool_named_twice_is_refused() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                **valid_spec(),
                "mcp_tools": [
                    {
                        "mcp_server_version_id": str(uuid4()),
                        "tools": ["search", "search"],
                    }
                ],
            }
        )


def test_too_many_servers_is_refused() -> None:
    bindings = [
        {"mcp_server_version_id": str(uuid4()), "tools": ["one"]}
        for _ in range(MAX_MCP_BINDINGS + 1)
    ]

    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**valid_spec(), "mcp_tools": bindings})


def test_too_many_tools_across_servers_is_refused() -> None:
    """An advertised tool is a tool in the model's list, and a model handed two
    hundred chooses worse than one handed twelve."""
    per_server = MAX_BOUND_MCP_TOOLS // 2 + 1
    bindings = [
        {
            "mcp_server_version_id": str(uuid4()),
            "tools": [f"tool{index}" for index in range(per_server)],
        }
        for _ in range(2)
    ]

    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**valid_spec(), "mcp_tools": bindings})


# -- and what publish does with it -------------------------------------------


@dataclass
class Servers:
    views: dict[UUID, McpBindingView]

    async def visible_versions(
        self, workspace_id: UUID, version_ids: Sequence[UUID]
    ) -> Sequence[McpBindingView]:
        del workspace_id
        return [self.views[key] for key in version_ids if key in self.views]


@dataclass
class Scopes:
    approved: OutboundScope

    async def workspace(self, workspace_id: UUID) -> OutboundScope:
        del workspace_id
        return self.approved


def view(
    *,
    server_name: str = "docs",
    host: str = "mcp.example.com",
    tool_names: tuple[str, ...] = ("search", "fetch"),
    active: bool = True,
) -> McpBindingView:
    return McpBindingView(
        mcp_server_id=uuid4(),
        version_id=uuid4(),
        server_name=server_name,
        host=host,
        tool_names=tool_names,
        active=active,
    )


@dataclass
class Publishing:
    catalog: AgentCatalog
    workspace_id: UUID
    actor: Actor
    agent_id: UUID
    revision: int

    async def publish(self) -> AgentVersion:
        result = await self.catalog.publish(
            self.workspace_id, self.actor, self.agent_id, self.revision, "req-3"
        )
        return result.version


async def publishing_with(
    bound: McpBindingView,
    *,
    tools: tuple[str, ...] = ("search",),
    allow: tuple[str, ...] = ("mcp.example.com",),
    reader: bool = True,
    visible: bool = True,
    write_policy: WritePolicy | None = WritePolicy.GOVERNANCE,
    role: Role = Role.DEVELOPER,
) -> Publishing:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = role
    catalog = AgentCatalog(
        store,
        scopes=Scopes(OutboundScope.of(allow)) if allow else None,
        mcp=(
            Servers({bound.version_id: bound} if visible else {}) if reader else None
        ),
    )
    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    spec: dict[str, object] = {
        **valid_spec(),
        "network": {"allow": list(allow)},
        "mcp_tools": [
            {
                "mcp_server_version_id": str(bound.version_id),
                "tools": list(tools),
                "write_policy": None if write_policy is None else write_policy.value,
            }
        ],
    }
    draft = await catalog.replace_draft(workspace_id, actor, agent.id, 1, spec, "req-2")
    return Publishing(catalog, workspace_id, actor, agent.id, draft.revision)


async def test_a_visible_active_version_inside_the_network_publishes() -> None:
    publishing = await publishing_with(view())

    published = await publishing.publish()

    assert published.version_number == 1


async def test_a_version_this_workspace_cannot_see_is_refused() -> None:
    publishing = await publishing_with(view(), visible=False)

    with pytest.raises(McpBindingUnavailable) as refused:
        await publishing.publish()

    assert "visible" in "".join(refused.value.reasons.values())


async def test_a_withdrawn_version_is_refused() -> None:
    bound = view(active=False)
    publishing = await publishing_with(bound)

    with pytest.raises(McpBindingUnavailable) as refused:
        await publishing.publish()

    assert "withdrawn" in refused.value.reasons[bound.version_id]


async def test_a_tool_the_snapshot_never_advertised_is_refused() -> None:
    """Publishing fixes the names, which is the part a person reviewed."""
    bound = view(tool_names=("search",))
    publishing = await publishing_with(bound, tools=("search", "deleteAll"))

    with pytest.raises(McpBindingUnavailable) as refused:
        await publishing.publish()

    assert "deleteAll" in refused.value.reasons[bound.version_id]


async def test_a_host_outside_this_agent_s_own_network_is_refused() -> None:
    bound = view(host="mcp.example.com")
    publishing = await publishing_with(bound, allow=("files.example.com",))

    with pytest.raises(McpBindingUnavailable) as refused:
        await publishing.publish()

    assert "network.allow" in refused.value.reasons[bound.version_id]


async def test_every_mcp_binding_must_choose_a_write_policy() -> None:
    """Stricter than the HTTP side, and the docstring says why: nothing tells
    this platform which MCP tools change something."""
    publishing = await publishing_with(view(), write_policy=None)

    with pytest.raises(WritePolicyNotChosen) as refused:
        await publishing.publish()

    assert refused.value.offending == {"docs": ("search",)}


async def test_a_developer_may_not_preauthorize_an_mcp_server() -> None:
    publishing = await publishing_with(
        view(), write_policy=WritePolicy.PREAUTHORIZED, role=Role.DEVELOPER
    )

    with pytest.raises(PreauthorizationNotPermitted) as refused:
        await publishing.publish()

    assert refused.value.tools == ("docs",)


async def test_a_workspace_admin_may_preauthorize_one() -> None:
    publishing = await publishing_with(
        view(), write_policy=WritePolicy.PREAUTHORIZED, role=Role.WORKSPACE_ADMIN
    )

    published = await publishing.publish()

    assert published.spec["mcp_tools"][0]["write_policy"] == "preauthorized"  # pyright: ignore[reportIndexIssue]


async def test_a_platform_with_no_mcp_catalog_refuses_rather_than_allows() -> None:
    bound = view()
    publishing = await publishing_with(bound, reader=False)

    with pytest.raises(McpBindingUnavailable) as refused:
        await publishing.publish()

    assert "configured" in refused.value.reasons[bound.version_id]
