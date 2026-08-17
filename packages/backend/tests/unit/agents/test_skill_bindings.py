"""What an Agent may bind, and what publish refuses.

Two things are pinned here.

The first is the widening promise, for the fourth time: `tools` made it,
`completion` made it, `context_budget` made it, and `skills` makes it again — a
spec that binds none normalizes to the same bytes with the same content hash, so
`schema_version` stays 1 and no published version is migrated.

The second is that every way a binding can be wrong is found at publish rather
than on the Run's first model round. That is the same argument `reject_unknown
_tools` makes: the author is the person who can fix it, and publish is the last
moment they are still holding it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from tiny_hermes.agents.application.service import (
    AgentCatalog,
    SkillBindingUnavailable,
    SkillBoundTwice,
    SkillSummaryBudgetExceeded,
)
from tiny_hermes.agents.domain.models import (
    MAX_SKILL_BINDINGS,
    AgentSpec,
    AgentVersion,
    normalize_agent_spec,
)
from tiny_hermes.agents.infrastructure.memory_store import MemoryAgentStore
from tiny_hermes.agents.ports.skills import SkillBindingView
from tiny_hermes.tenancy.domain.models import Actor, Role

from .test_agent_models import valid_spec
from .test_model_policy import DETERMINISTIC_HASH

# -- the widening ------------------------------------------------------------


def test_a_spec_that_binds_no_skill_hashes_to_what_it_did() -> None:
    spec = AgentSpec.model_validate(valid_spec())
    document, content_hash = normalize_agent_spec(spec)

    assert content_hash == DETERMINISTIC_HASH
    assert "skills" not in document
    assert spec.schema_version == 1
    assert spec.skills == ()


def test_a_binding_survives_normalization() -> None:
    version_id = uuid4()
    spec = AgentSpec.model_validate(
        {**valid_spec(), "skills": [{"skill_version_id": str(version_id)}]}
    )

    document, content_hash = normalize_agent_spec(spec)

    assert document["skills"] == [{"skill_version_id": str(version_id)}]
    assert content_hash != DETERMINISTIC_HASH


# -- what an author may write ------------------------------------------------


def test_a_binding_names_a_version_and_nothing_else() -> None:
    """Red line two. A binding by name would mean an upload to the catalog
    changes what a published Agent does, which is the one thing an immutable
    AgentVersion is for."""
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**valid_spec(), "skills": [{"skill": "house-style"}]})


def test_seventeen_bindings_are_refused() -> None:
    bindings = [{"skill_version_id": str(uuid4())} for _ in range(MAX_SKILL_BINDINGS + 1)]

    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**valid_spec(), "skills": bindings})


def test_the_same_version_twice_is_refused_by_the_spec_itself() -> None:
    """This one needs no catalog to see, so it does not wait for one."""
    version_id = str(uuid4())

    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                **valid_spec(),
                "skills": [{"skill_version_id": version_id}] * 2,
            }
        )


# -- and what publish does with it -------------------------------------------


@dataclass
class Skills:
    """The smallest reader the publish check needs."""

    views: dict[UUID, SkillBindingView]

    async def visible_versions(
        self, workspace_id: UUID, version_ids: Sequence[UUID]
    ) -> Sequence[SkillBindingView]:
        del workspace_id
        return [self.views[key] for key in version_ids if key in self.views]


def view(
    *,
    name: str = "house-style",
    description: str = "How this company writes.",
    active: bool = True,
    blocked_by_scan: bool = False,
    skill_id: UUID | None = None,
) -> SkillBindingView:
    return SkillBindingView(
        skill_id=skill_id or uuid4(),
        version_id=uuid4(),
        name=name,
        description=description,
        active=active,
        blocked_by_scan=blocked_by_scan,
    )


@dataclass
class Publishing:
    """A draft that binds skills, and everything needed to publish it."""

    catalog: AgentCatalog
    workspace_id: UUID
    actor: Actor
    agent_id: UUID
    revision: int
    skills: Skills

    async def publish(self) -> AgentVersion:
        result = await self.catalog.publish(
            self.workspace_id, self.actor, self.agent_id, self.revision, "req-3"
        )
        return result.version


async def publishing_with(
    *views: SkillBindingView,
    reader: bool = True,
    budget: object | None = None,
) -> Publishing:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    skills = Skills({entry.version_id: entry for entry in views})
    catalog = AgentCatalog(store, skills=skills if reader else None)
    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    spec: dict[str, object] = {
        **valid_spec(),
        "skills": [{"skill_version_id": str(entry.version_id)} for entry in views],
    }
    if budget is not None:
        spec["context_budget"] = budget
    draft = await catalog.replace_draft(workspace_id, actor, agent.id, 1, spec, "req-2")
    return Publishing(catalog, workspace_id, actor, agent.id, draft.revision, skills)


async def test_a_visible_active_version_publishes() -> None:
    bound = view()
    publishing = await publishing_with(bound)

    published = await publishing.publish()

    assert published.version_number == 1
    assert published.spec["skills"] == [{"skill_version_id": str(bound.version_id)}]


async def test_a_version_from_another_workspace_is_simply_not_there() -> None:
    """Not "forbidden": a draft may not learn that a version it cannot see
    exists somewhere else, which is the catalog's own 404-not-403 rule."""
    elsewhere = view()
    publishing = await publishing_with(elsewhere)
    publishing.skills.views.clear()  # the version moved out of sight

    with pytest.raises(SkillBindingUnavailable) as refused:
        await publishing.publish()

    assert refused.value.reasons == {
        elsewhere.version_id: "no such skill version is visible here"
    }


async def test_a_withdrawn_version_may_not_be_newly_bound() -> None:
    """§15.1: content stays readable for Runs already bound to it, and nothing
    new may bind it. Publishing is the "new" that check is for."""
    withdrawn = view(active=False)
    publishing = await publishing_with(withdrawn)

    with pytest.raises(SkillBindingUnavailable) as refused:
        await publishing.publish()

    assert "withdrawn" in refused.value.reasons[withdrawn.version_id]


async def test_a_version_the_scan_blocks_may_not_be_bound() -> None:
    blocked = view(blocked_by_scan=True)
    publishing = await publishing_with(blocked)

    with pytest.raises(SkillBindingUnavailable) as refused:
        await publishing.publish()

    assert "blocking scan finding" in refused.value.reasons[blocked.version_id]


async def test_every_failing_binding_is_named_at_once() -> None:
    """An author binding four skills should not publish four times to learn
    about all four."""
    gone = view(name="a")
    withdrawn = view(name="b", active=False)
    blocked = view(name="c", blocked_by_scan=True)
    fine = view(name="d")
    publishing = await publishing_with(gone, withdrawn, blocked, fine)
    del publishing.skills.views[gone.version_id]

    with pytest.raises(SkillBindingUnavailable) as refused:
        await publishing.publish()

    assert set(refused.value.reasons) == {
        gone.version_id,
        withdrawn.version_id,
        blocked.version_id,
    }


async def test_two_versions_of_one_skill_are_refused() -> None:
    """The spec cannot see this one — both bindings are distinct version ids —
    so it is refused where the catalog can say they are the same skill."""
    skill_id = uuid4()
    first = view(name="house-style", skill_id=skill_id)
    second = view(name="house-style", skill_id=skill_id)
    publishing = await publishing_with(first, second)

    with pytest.raises(SkillBoundTwice) as refused:
        await publishing.publish()

    assert refused.value.name == "house-style"


async def test_a_catalog_with_no_reader_cannot_publish_a_binding() -> None:
    """Refused rather than skipped. An unchecked binding is a Run that fails on
    its first round, which is precisely what this check exists to prevent."""
    publishing = await publishing_with(view(), reader=False)

    with pytest.raises(SkillBindingUnavailable):
        await publishing.publish()


async def test_a_spec_binding_nothing_never_asks() -> None:
    """A catalog with no skill reader keeps publishing everything it could
    publish before this check existed."""
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store)
    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    draft = await catalog.replace_draft(
        workspace_id, actor, agent.id, 1, valid_spec(), "req-2"
    )

    published = await catalog.publish(
        workspace_id, actor, agent.id, draft.revision, "req-3"
    )

    assert "skills" not in published.version.spec


# -- the summary budget ------------------------------------------------------


async def test_summaries_that_do_not_fit_the_segment_are_refused_with_numbers() -> None:
    """Sixteen skills at 200 characters is about 3200 characters against a
    1536-token ceiling, so this arithmetic decides real drafts. The refusal
    lists every summary, the rule `ContextBudgetUnsatisfied` set."""
    views = [
        view(name=f"skill-{index}", description="A sentence about it. " * 40)
        for index in range(MAX_SKILL_BINDINGS)
    ]
    publishing = await publishing_with(*views)

    with pytest.raises(SkillSummaryBudgetExceeded) as refused:
        await publishing.publish()

    assert refused.value.allowance == 1_536
    assert len(refused.value.estimates) == MAX_SKILL_BINDINGS
    assert sum(refused.value.estimates.values()) > refused.value.allowance


async def test_a_few_short_summaries_fit() -> None:
    publishing = await publishing_with(
        view(name="a", description="How this company writes."),
        view(name="b", description="How this company reviews."),
    )

    published = await publishing.publish()

    assert published.version_number == 1


async def test_an_author_who_widened_the_segment_is_measured_against_that() -> None:
    """The ceiling is the one this spec resolves to, not the platform default,
    for the same reason `_check_context_budget` resolves before it measures."""
    long_enough = view(description="A sentence about it. " * 40)
    publishing = await publishing_with(
        long_enough,
        budget={"segments": [{"segment": "skill_summaries", "max_tokens": 64}]},
    )

    with pytest.raises(SkillSummaryBudgetExceeded) as refused:
        await publishing.publish()

    assert refused.value.allowance == 64
