"""The two ceilings on loading, and the refusals that say so out loud.

Design §10.1 puts a size on one load and a count on one Run. Both are refusals
with a reason in them rather than silent failures, for the same reason the
context planner names what it trimmed: a model that is told nothing about a
limit it just hit will try again, and a person reading the Run afterwards
cannot tell a refusal from an empty file.

The size limit refuses rather than truncates, which is the rule M2A-2 already
settled for trimming — a model handed half a document cannot tell that it is
holding half, and will act on the half it got.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from tiny_hermes.agents.domain.models import AgentSpec
from tiny_hermes.runs.application.tool_answers import answer_skill_load
from tiny_hermes.runs.domain.models import (
    BoundSkill,
    BudgetSummary,
    RunEventType,
    ToolCallBlock,
)
from tiny_hermes.runs.ports.store import ExecutionContext
from tiny_hermes.tools.domain.registry import MAX_SKILL_FILE_BYTES, MAX_SKILL_LOADS

VERSION_ID = uuid4()
SPEC = AgentSpec.model_validate(
    {
        "schema_version": 1,
        "personality": "You are concise.",
        "model_policy": {"provider": "deterministic", "scenario": "skill_once"},
        "tools": ["skill.load"],
        "limits": {
            "max_execution_seconds": 900,
            "max_elapsed_seconds": 86400,
            "max_model_calls": 20,
            "max_tool_calls": 50,
            "max_derived_retries": 3,
        },
    }
)


class Library:
    """One file, however big the test needs it to be."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.reads: list[tuple[UUID, str]] = []

    async def read_file(self, version_id: UUID, path: str) -> str | None:
        self.reads.append((version_id, path))
        return self.text


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
    return ToolCallBlock(call_id="skill-1", name="skill.load", arguments=arguments)


async def test_a_bound_skill_comes_back_with_the_event_that_records_it() -> None:
    library = Library("The rollout runbook.")

    answer, event = await answer_skill_load(library, context(), call(skill="rollout"), [])

    assert answer.failed is False
    assert answer.output == "The rollout runbook."
    assert library.reads == [(VERSION_ID, "SKILL.md")]
    assert event is not None
    assert event.event_type is RunEventType.SKILL_LOADED
    assert event.payload == {
        "skill": "rollout",
        "path": "SKILL.md",
        "skill_version_id": str(VERSION_ID),
        "bytes": len("The rollout runbook."),
    }


async def test_a_file_over_the_size_limit_is_refused_and_told_how_big_it_is() -> None:
    """Its size is the one fact that makes the refusal actionable.

    Without it the model knows only that something went wrong, and its next
    move is to ask for the same file again.
    """
    oversized = "x" * (MAX_SKILL_FILE_BYTES + 1)

    answer, event = await answer_skill_load(
        Library(oversized), context(), call(skill="rollout"), []
    )

    assert answer.failed is True
    assert str(MAX_SKILL_FILE_BYTES + 1) in answer.output
    assert str(MAX_SKILL_FILE_BYTES) in answer.output
    assert oversized not in answer.output
    assert event is None


async def test_the_size_is_counted_in_bytes_and_not_in_characters() -> None:
    """A document of CJK prose is three times the size its length suggests."""
    just_over = "写" * (MAX_SKILL_FILE_BYTES // 3 + 1)
    assert len(just_over) < MAX_SKILL_FILE_BYTES

    answer, _ = await answer_skill_load(Library(just_over), context(), call(skill="rollout"), [])

    assert answer.failed is True


async def test_the_run_s_ninth_load_is_refused_by_name() -> None:
    already = [uuid4() for _ in range(MAX_SKILL_LOADS)]

    answer, event = await answer_skill_load(
        Library("text"), context(), call(skill="rollout"), already
    )

    assert answer.failed is True
    assert str(MAX_SKILL_LOADS) in answer.output
    assert event is None


async def test_the_eighth_load_still_goes_through() -> None:
    already = [uuid4() for _ in range(MAX_SKILL_LOADS - 1)]

    answer, event = await answer_skill_load(
        Library("text"), context(), call(skill="rollout"), already
    )

    assert answer.failed is False
    assert event is not None


async def test_a_refusal_does_not_spend_the_run_s_allowance() -> None:
    """The ceiling is on documents loaded, not on calls attempted.

    A model that mistypes a skill name four times has read nothing, and
    charging it four of its eight would spend the limit on typing.
    """
    library = Library("text")
    loaded: list[UUID] = []

    for _ in range(4):
        answer, event = await answer_skill_load(library, context(), call(skill="rollback"), loaded)
        assert answer.failed is True
        assert event is None

    answer, event = await answer_skill_load(library, context(), call(skill="rollout"), loaded)
    assert answer.failed is False
    assert event is not None


async def test_a_skill_the_version_did_not_bind_is_not_read_at_all() -> None:
    """Refused before the catalog is touched. What the model names is not a key."""
    library = Library("someone else's document")

    answer, event = await answer_skill_load(library, context(), call(skill="postmortem"), [])

    assert answer.failed is True
    assert "tool_not_authorized" in answer.output
    assert library.reads == []
    assert event is None


async def test_an_agent_that_did_not_bind_the_tool_cannot_be_made_to_load() -> None:
    """Even with skills bound: the two are separate things a Version says.

    Binding a skill says which documents exist for this Agent. Binding
    `skill.load` says the model may go and read them.
    """
    without = SPEC.model_copy(update={"tools": ()})

    answer, event = await answer_skill_load(
        Library("text"), context(spec=without), call(skill="rollout"), []
    )

    assert answer.failed is True
    assert "tool_not_authorized" in answer.output
    assert event is None


async def test_a_deployment_with_no_catalog_says_so_rather_than_crashing() -> None:
    answer, event = await answer_skill_load(None, context(), call(skill="rollout"), [])

    assert answer.failed is True
    assert "catalog" in answer.output
    assert event is None
