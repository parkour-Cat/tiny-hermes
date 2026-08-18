"""What an Agent's proposal costs it, and what it is told it did.

Design §15.3 gives an Agent the right to suggest and nothing else. Two halves
are tested here: the argument shape `skill.propose` accepts, and the answer the
Run gets back — which has to say plainly that nothing changed, because a model
that reads "proposal opened" as "skill updated" will spend the rest of the Run
acting on a document that does not exist.

Patching is scoped like loading: the skill named must be one this Run's Version
bound, and the base is the exact version the Run was given. A skill the Agent
was never shown is not one it is in a position to rewrite.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from tiny_hermes.agents.domain.models import AgentSpec
from tiny_hermes.runs.application.tool_answers import answer_skill_propose
from tiny_hermes.runs.domain.models import (
    BoundSkill,
    BudgetSummary,
    RunEventType,
    ToolCallBlock,
)
from tiny_hermes.runs.ports.proposals import ProposalOutcome
from tiny_hermes.runs.ports.store import ExecutionContext
from tiny_hermes.tools.domain.registry import (
    IMPLEMENTED_TOOLS,
    MAX_PROPOSAL_FILES,
    PLATFORM_TOOLS,
    RefusalReason,
    ToolRefused,
    authorize,
    schemas_for,
    skill_propose_of,
)

VERSION_ID = uuid4()
SPEC = AgentSpec.model_validate(
    {
        "schema_version": 1,
        "personality": "You are concise.",
        "model_policy": {"provider": "deterministic", "scenario": "complete"},
        "tools": ["skill.propose"],
        "limits": {
            "max_execution_seconds": 900,
            "max_elapsed_seconds": 86400,
            "max_model_calls": 20,
            "max_tool_calls": 50,
            "max_derived_retries": 3,
        },
    }
)
FILES = [{"path": "SKILL.md", "content": "---\nname: rollout\ndescription: d\n---\n"}]


class Proposals:
    """The catalog's answer, and a record of what it was asked."""

    def __init__(self, outcome: ProposalOutcome | None = None) -> None:
        self.outcome = outcome or ProposalOutcome(proposal_id=uuid4())
        self.calls: list[tuple[UUID, UUID | None, int]] = []

    async def propose(
        self,
        *,
        run_id: UUID,
        skill_version_id: UUID | None,
        files: Sequence[tuple[str, str]],
    ) -> ProposalOutcome:
        self.calls.append((run_id, skill_version_id, len(files)))
        return self.outcome


def context(**overrides: object) -> ExecutionContext:
    fields: dict[str, object] = {
        "run_id": uuid4(),
        "state_version": 1,
        "spec": SPEC,
        "history": (),
        "cancel_requested": False,
        "pause_requested": False,
        "budget": BudgetSummary(
            max_execution_seconds=900,
            consumed_execution_ms=0,
            max_elapsed_seconds=86_400,
            elapsed_deadline_at=datetime.now(UTC) + timedelta(days=1),
            max_model_calls=20,
            consumed_model_calls=1,
            max_tool_calls=50,
            consumed_tool_calls=0,
            max_tokens=None,
            consumed_tokens=0,
            max_derived_retries=3,
            derived_retry_count=0,
        ),
        "skills": (
            BoundSkill(skill_version_id=VERSION_ID, name="rollout", description="how to ship"),
        ),
    }
    fields.update(overrides)
    return ExecutionContext(**fields)  # type: ignore[arg-type]


def call(**arguments: object) -> ToolCallBlock:
    return ToolCallBlock(call_id="propose-1", name="skill.propose", arguments=arguments)


# -- the call's shape -------------------------------------------------------


def test_it_is_a_platform_tool_like_the_other_two() -> None:
    assert "skill.propose" in IMPLEMENTED_TOOLS
    assert "skill.propose" in PLATFORM_TOOLS
    with pytest.raises(ToolRefused) as refusal:
        authorize(bound=("skill.propose",), call=call(files=FILES))
    assert refusal.value.reason is RefusalReason.UNKNOWN_TOOL


def test_the_model_is_told_it_exists_only_when_it_is_bound() -> None:
    names = [schema["function"]["name"] for schema in schemas_for(("skill.propose",))]
    assert names == ["skill.propose"]
    assert "skill.propose" not in [
        schema["function"]["name"] for schema in schemas_for(("skill.load",))
    ]


def test_a_whole_package_comes_through_in_order() -> None:
    asked = skill_propose_of(
        call(
            files=[
                {"path": "SKILL.md", "content": "a"},
                {"path": "reference/rollback.md", "content": "b"},
            ],
            skill="rollout",
        )
    )

    assert asked.files == (("SKILL.md", "a"), ("reference/rollback.md", "b"))
    assert asked.skill == "rollout"


def test_naming_no_skill_is_how_a_new_one_is_proposed() -> None:
    assert skill_propose_of(call(files=FILES)).skill is None


def test_an_empty_file_is_a_file_and_a_missing_one_is_not() -> None:
    """`content: ""` is a deliberate empty file; a missing key is a mistake."""
    assert skill_propose_of(call(files=[{"path": "a.md", "content": ""}])).files == (
        ("a.md", ""),
    )
    with pytest.raises(ToolRefused):
        skill_propose_of(call(files=[{"path": "a.md"}]))


@pytest.mark.parametrize(
    "files",
    [
        [],
        "SKILL.md",
        [{"path": 1, "content": "a"}],
        ["SKILL.md"],
        [{"path": "a.md", "content": 3}],
    ],
)
def test_files_that_are_not_a_file_list_are_refused(files: object) -> None:
    with pytest.raises(ToolRefused) as refusal:
        skill_propose_of(call(files=files))

    assert refusal.value.reason is RefusalReason.INVALID_ARGUMENTS


def test_more_files_than_a_package_may_hold_is_refused_by_count() -> None:
    """Told here rather than by the parser, so the number the model passed is
    in the refusal it reads."""
    many = [{"path": f"{index}.md", "content": "x"} for index in range(MAX_PROPOSAL_FILES + 1)]

    with pytest.raises(ToolRefused) as refusal:
        skill_propose_of(call(files=many))

    assert str(MAX_PROPOSAL_FILES + 1) in refusal.value.detail


def test_an_unknown_argument_is_refused() -> None:
    with pytest.raises(ToolRefused):
        skill_propose_of(call(files=FILES, approve=True))


# -- what the Run gets back -------------------------------------------------


async def test_a_proposal_against_a_bound_skill_uses_the_version_this_run_holds() -> None:
    proposals = Proposals()

    answer, event = await answer_skill_propose(
        proposals, context(), call(files=FILES, skill="rollout")
    )

    assert answer.failed is False
    assert proposals.calls[0][1] == VERSION_ID
    assert event is not None
    assert event.event_type is RunEventType.SKILL_PROPOSED
    assert event.payload["skill"] == "rollout"


async def test_the_answer_says_that_nothing_has_changed() -> None:
    """The one sentence this tool exists to make true.

    A model told only "ok" would carry on as though the skill it proposed is
    now the skill it has.
    """
    outcome = ProposalOutcome(proposal_id=uuid4())

    answer, _ = await answer_skill_propose(
        Proposals(outcome), context(), call(files=FILES, skill="rollout")
    )

    assert str(outcome.proposal_id) in answer.output
    assert "Nothing has changed" in answer.output
    assert "approves" in answer.output


async def test_a_new_skill_needs_no_binding() -> None:
    proposals = Proposals()

    answer, _ = await answer_skill_propose(proposals, context(), call(files=FILES))

    assert answer.failed is False
    assert proposals.calls[0][1] is None


async def test_a_skill_this_run_did_not_bind_cannot_be_patched() -> None:
    proposals = Proposals()

    answer, event = await answer_skill_propose(
        proposals, context(), call(files=FILES, skill="postmortem")
    )

    assert answer.failed is True
    assert "tool_not_authorized" in answer.output
    assert proposals.calls == []
    assert event is None


async def test_an_agent_that_did_not_bind_the_tool_cannot_propose() -> None:
    without = SPEC.model_copy(update={"tools": ()})

    answer, event = await answer_skill_propose(
        Proposals(), context(spec=without), call(files=FILES)
    )

    assert answer.failed is True
    assert "tool_not_authorized" in answer.output
    assert event is None


async def test_the_catalog_s_refusal_is_passed_through_as_a_sentence() -> None:
    """Every reason a proposal is turned away is something the model could fix
    by writing different files, so it gets the words rather than a code."""
    refused = ProposalOutcome(refusal="a skill package needs a SKILL.md at its root")

    answer, event = await answer_skill_propose(
        Proposals(refused), context(), call(files=FILES)
    )

    assert answer.failed is True
    assert "needs a SKILL.md" in answer.output
    assert event is None


async def test_a_deployment_with_no_catalog_says_so() -> None:
    answer, event = await answer_skill_propose(None, context(), call(files=FILES))

    assert answer.failed is True
    assert "catalog" in answer.output
    assert event is None
