"""What an Agent may call at somebody else's API, and what publish refuses.

Two things pinned here, the same two the skill bindings next door pin.

The widening promise, for the sixth time: a spec that binds no HTTP tool
normalizes to the same bytes with the same content hash, so `schema_version`
stays 1 and no published version is migrated.

And every way a binding can be wrong found at publish rather than on the Run's
first model round. One of those ways is new: the host was inside the
*workspace* scope when the tool was registered, and whether this *Agent* may
reach it is a different question asked of a different layer. §16.5 lets a layer
narrow, so an Agent that binds a tool it may not connect to is an Agent that
publishes cleanly and refuses on first use.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from tiny_hermes.agents.application.service import (
    AgentCatalog,
    HttpToolBindingUnavailable,
    PreauthorizationNotPermitted,
    WritePolicyNotChosen,
)
from tiny_hermes.agents.domain.models import (
    MAX_BOUND_OPERATIONS,
    MAX_HTTP_TOOL_BINDINGS,
    AgentSpec,
    AgentVersion,
    WritePolicy,
    normalize_agent_spec,
)
from tiny_hermes.agents.infrastructure.memory_store import MemoryAgentStore
from tiny_hermes.agents.ports.http_tools import HttpToolBindingView
from tiny_hermes.outbound.domain.scope import OutboundScope
from tiny_hermes.tenancy.domain.models import Actor, Role

from .test_agent_models import valid_spec
from .test_model_policy import DETERMINISTIC_HASH

# -- the widening ------------------------------------------------------------


def test_a_spec_that_binds_no_http_tool_hashes_to_what_it_did() -> None:
    spec = AgentSpec.model_validate(valid_spec())
    document, content_hash = normalize_agent_spec(spec)

    assert content_hash == DETERMINISTIC_HASH
    assert "http_tools" not in document
    assert spec.schema_version == 1
    assert spec.http_tools == ()


def test_a_binding_survives_normalization() -> None:
    version_id = uuid4()
    spec = AgentSpec.model_validate(
        {
            **valid_spec(),
            "http_tools": [
                {"http_tool_version_id": str(version_id), "operations": ["listOrders"]}
            ],
        }
    )

    document, content_hash = normalize_agent_spec(spec)

    assert document["http_tools"] == [
        {
            "http_tool_version_id": str(version_id),
            "operations": ["listOrders"],
            # Present and null for a read-only binding: §16.3's choice is only
            # required where something could write.
            "write_policy": None,
        }
    ]
    assert content_hash != DETERMINISTIC_HASH


# -- what an author may write ------------------------------------------------


def test_a_binding_names_a_version_and_nothing_else() -> None:
    """The same red line as a skill binding. Naming the tool would mean the API
    owner's next export changes what a published Agent does."""
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {**valid_spec(), "http_tools": [{"tool": "orders", "operations": ["x"]}]}
        )


def test_binding_no_operation_is_refused_rather_than_read_as_all_of_them() -> None:
    """A document with forty operations bound by an author who wanted two would
    be thirty-eight capabilities nobody chose."""
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                **valid_spec(),
                "http_tools": [
                    {"http_tool_version_id": str(uuid4()), "operations": []}
                ],
            }
        )


def test_the_same_version_twice_is_refused_by_the_spec_itself() -> None:
    version_id = str(uuid4())

    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                **valid_spec(),
                "http_tools": [
                    {"http_tool_version_id": version_id, "operations": ["a"]},
                    {"http_tool_version_id": version_id, "operations": ["b"]},
                ],
            }
        )


def test_one_operation_named_twice_in_one_binding_is_refused() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                **valid_spec(),
                "http_tools": [
                    {
                        "http_tool_version_id": str(uuid4()),
                        "operations": ["listOrders", "listOrders"],
                    }
                ],
            }
        )


def test_too_many_tools_is_refused() -> None:
    bindings = [
        {"http_tool_version_id": str(uuid4()), "operations": ["one"]}
        for _ in range(MAX_HTTP_TOOL_BINDINGS + 1)
    ]

    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**valid_spec(), "http_tools": bindings})


def test_too_many_operations_across_tools_is_refused() -> None:
    """The ceiling that matters: an operation is a tool in the model's list,
    and a model handed two hundred tools chooses worse than one handed twelve."""
    per_tool = MAX_BOUND_OPERATIONS // 2 + 1
    bindings = [
        {
            "http_tool_version_id": str(uuid4()),
            "operations": [f"op{index}" for index in range(per_tool)],
        }
        for _ in range(2)
    ]

    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**valid_spec(), "http_tools": bindings})


# -- and what publish does with it -------------------------------------------


@dataclass
class HttpTools:
    """The smallest reader the publish check needs."""

    views: dict[UUID, HttpToolBindingView]

    async def visible_versions(
        self, workspace_id: UUID, version_ids: Sequence[UUID]
    ) -> Sequence[HttpToolBindingView]:
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
    tool_name: str = "orders",
    host: str = "api.example.com",
    operation_ids: tuple[str, ...] = ("listOrders", "createOrder"),
    write_operation_ids: tuple[str, ...] = ("createOrder",),
    active: bool = True,
) -> HttpToolBindingView:
    return HttpToolBindingView(
        http_tool_id=uuid4(),
        version_id=uuid4(),
        tool_name=tool_name,
        host=host,
        operation_ids=operation_ids,
        write_operation_ids=write_operation_ids,
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
    bound: HttpToolBindingView,
    *,
    operations: tuple[str, ...] = ("listOrders",),
    allow: tuple[str, ...] = ("api.example.com",),
    reader: bool = True,
    visible: bool = True,
    write_policy: WritePolicy | None = None,
    role: Role = Role.DEVELOPER,
) -> Publishing:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = role
    catalog = AgentCatalog(
        store,
        scopes=Scopes(OutboundScope.of(allow)) if allow else None,
        http_tools=(
            HttpTools({bound.version_id: bound} if visible else {})
            if reader
            else None
        ),
    )
    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    spec: dict[str, object] = {
        **valid_spec(),
        "network": {"allow": list(allow)},
        "http_tools": [
            {
                "http_tool_version_id": str(bound.version_id),
                "operations": list(operations),
                "write_policy": None if write_policy is None else write_policy.value,
            }
        ],
    }
    draft = await catalog.replace_draft(workspace_id, actor, agent.id, 1, spec, "req-2")
    return Publishing(catalog, workspace_id, actor, agent.id, draft.revision)


async def test_a_visible_active_version_inside_the_network_publishes() -> None:
    bound = view()
    publishing = await publishing_with(bound)

    published = await publishing.publish()

    assert published.spec["http_tools"] == [
        {
            "http_tool_version_id": str(bound.version_id),
            "operations": ["listOrders"],
            "write_policy": None,
        }
    ]


async def test_a_version_this_workspace_cannot_see_is_refused() -> None:
    # The reader answers about nothing, which is what another workspace's
    # version looks like from here — deliberately the same as one that does
    # not exist.
    publishing = await publishing_with(view(), visible=False)

    with pytest.raises(HttpToolBindingUnavailable) as refused:
        await publishing.publish()

    assert "visible" in "".join(refused.value.reasons.values())


async def test_a_withdrawn_version_is_refused() -> None:
    bound = view(active=False)
    publishing = await publishing_with(bound)

    with pytest.raises(HttpToolBindingUnavailable) as refused:
        await publishing.publish()

    assert "withdrawn" in refused.value.reasons[bound.version_id]


async def test_an_operation_the_document_does_not_declare_is_refused() -> None:
    bound = view(operation_ids=("listOrders",), write_operation_ids=())
    publishing = await publishing_with(bound, operations=("listOrders", "deleteAll"))

    with pytest.raises(HttpToolBindingUnavailable) as refused:
        await publishing.publish()

    assert "deleteAll" in refused.value.reasons[bound.version_id]


async def test_a_host_outside_this_agent_s_own_network_is_refused() -> None:
    """The workspace approved it — that is why the tool exists. This Agent did
    not, and §16.5 lets a layer narrow."""
    bound = view(host="api.example.com")
    publishing = await publishing_with(bound, allow=("files.example.com",))

    with pytest.raises(HttpToolBindingUnavailable) as refused:
        await publishing.publish()

    assert "network.allow" in refused.value.reasons[bound.version_id]


async def test_binding_a_tool_while_asking_for_no_network_is_refused() -> None:
    """An Agent that never asked for the network does not get it because its
    workspace has some — and a bound tool it cannot reach is worse than none."""
    bound = view()
    publishing = await publishing_with(bound, allow=())

    with pytest.raises(HttpToolBindingUnavailable):
        await publishing.publish()


async def test_a_platform_with_no_http_catalog_refuses_rather_than_allows() -> None:
    bound = view()
    publishing = await publishing_with(bound, reader=False)

    with pytest.raises(HttpToolBindingUnavailable) as refused:
        await publishing.publish()

    assert "configured" in refused.value.reasons[bound.version_id]


# -- and the choice §16.3 will not let a version skip ------------------------


async def test_binding_a_write_without_choosing_what_happens_is_refused() -> None:
    """All three answers are defensible and none is a safe default: silently
    disabling surprises the author, silently pre-authorizing grants a
    permission nobody granted, silently escalating makes administrators a
    queue. So the version has to say."""
    bound = view()
    publishing = await publishing_with(bound, operations=("createOrder",))

    with pytest.raises(WritePolicyNotChosen) as refused:
        await publishing.publish()

    assert refused.value.offending == {"orders": ("createOrder",)}


async def test_a_read_only_binding_needs_no_choice() -> None:
    """The check is about what could write, not about HTTP tools in general."""
    bound = view()
    publishing = await publishing_with(bound, operations=("listOrders",))

    published = await publishing.publish()

    assert published.version_number == 1


@pytest.mark.parametrize(
    "policy", [WritePolicy.DISABLED, WritePolicy.GOVERNANCE]
)
async def test_a_developer_may_choose_disabled_or_governance(
    policy: WritePolicy,
) -> None:
    publishing = await publishing_with(
        view(), operations=("createOrder",), write_policy=policy
    )

    published = await publishing.publish()

    assert published.spec["http_tools"][0]["write_policy"] == policy.value  # pyright: ignore[reportIndexIssue]


async def test_a_developer_may_not_preauthorize_their_own_writes() -> None:
    """§16.3 calls it "a narrow scope a workspace administrator approved". A
    developer publishing one would be granting themselves that approval."""
    publishing = await publishing_with(
        view(),
        operations=("createOrder",),
        write_policy=WritePolicy.PREAUTHORIZED,
        role=Role.DEVELOPER,
    )

    with pytest.raises(PreauthorizationNotPermitted) as refused:
        await publishing.publish()

    assert refused.value.tools == ("orders",)


async def test_a_workspace_admin_may_preauthorize() -> None:
    publishing = await publishing_with(
        view(),
        operations=("createOrder",),
        write_policy=WritePolicy.PREAUTHORIZED,
        role=Role.WORKSPACE_ADMIN,
    )

    published = await publishing.publish()

    assert published.spec["http_tools"][0]["write_policy"] == "preauthorized"  # pyright: ignore[reportIndexIssue]
