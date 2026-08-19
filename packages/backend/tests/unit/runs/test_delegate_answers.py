"""What a round does when the model calls `agent.delegate`.

§13's creation path seen from the Run's side. Two claims are worth stating
before the tests, because both are about what this function deliberately does
**not** do.

**It decides almost nothing.** Depth, bindings, the parallel ceiling and the
scope intersection are all settled where the children are created, and these
tests assert that the request arrives there unchanged rather than that it was
adjusted on the way. The one check that does live here is §10.2's binding
check, which every platform tool repeats for itself: a model asking for a tool
its Version did not bind is refused whether or not it was ever shown the
schema.

**It does not make the parent wait.** That is the next step of the plan, and
until it exists the result has to say so in words a model will act on. A parent
told only "started 2" would go on to read results that do not exist yet.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from tiny_hermes.agents.domain.models import AgentSpec, DeterministicModelPolicy
from tiny_hermes.runs.application.tool_answers import answer_agent_delegate
from tiny_hermes.runs.domain.models import RunEventType, ToolCallBlock
from tiny_hermes.runs.ports.children import (
    DelegatedChild,
    DelegationRequest,
    DelegationResult,
)
from tiny_hermes.runs.ports.store import BudgetSummary, ExecutionContext


@dataclass
class Children:
    """A stand-in that records what it was asked for, and answers as told."""

    result: DelegationResult = field(
        default_factory=lambda: DelegationResult(
            children=(
                DelegatedChild(run_id=uuid4(), session_id=uuid4(), alias="reader"),
            )
        )
    )
    asked: list[tuple[UUID, tuple[DelegationRequest, ...]]] = field(
        default_factory=list[tuple[UUID, tuple[DelegationRequest, ...]]]
    )

    async def delegate(
        self, *, parent_run_id: UUID, requests: tuple[DelegationRequest, ...]
    ) -> DelegationResult:
        self.asked.append((parent_run_id, requests))
        return self.result


def _context(*tools: str) -> ExecutionContext:
    return ExecutionContext(
        run_id=uuid4(),
        state_version=1,
        spec=AgentSpec(
            personality="A coordinator.",
            model_policy=DeterministicModelPolicy(),
            tools=tools,
        ),
        history=(),
        cancel_requested=False,
        pause_requested=False,
        budget=BudgetSummary(
            max_execution_seconds=900,
            consumed_execution_ms=0,
            max_elapsed_seconds=86_400,
            elapsed_deadline_at=datetime.now(UTC) + timedelta(days=1),
            max_model_calls=20,
            consumed_model_calls=0,
            max_tool_calls=50,
            consumed_tool_calls=0,
            max_tokens=None,
            consumed_tokens=0,
            max_derived_retries=3,
            derived_retry_count=0,
        ),
    )


def _call(**arguments: object) -> ToolCallBlock:
    return ToolCallBlock(call_id="d-1", name="agent.delegate", arguments=arguments)


async def test_an_unbound_agent_is_refused_before_anything_is_asked() -> None:
    """§10.2's second step. The schema list is not the control, the binding is."""
    children = Children()
    answered, event = await answer_agent_delegate(
        children,
        _context("shell.exec"),
        _call(children=[{"alias": "reader", "instruction": "Read it."}]),
    )

    assert answered.failed
    assert "not_authorized" in answered.output
    assert event is None
    # The refusal is the point: nothing reached the creation path at all.
    assert children.asked == []


async def test_what_the_model_asked_for_reaches_the_creation_path_unchanged() -> None:
    """Two children, in the order the model named them, with their own text.

    Asserted rather than assumed because this function is the only place the
    call is turned into requests. A rewriting here — a deduplicated alias, a
    trimmed instruction, a reordering — would be a delegation the author
    reading the transcript did not make.
    """
    children = Children(
        result=DelegationResult(
            children=(
                DelegatedChild(run_id=uuid4(), session_id=uuid4(), alias="reader"),
                DelegatedChild(run_id=uuid4(), session_id=uuid4(), alias="checker"),
            )
        )
    )
    context = _context("agent.delegate")

    answered, event = await answer_agent_delegate(
        children,
        context,
        _call(
            children=[
                {"alias": "reader", "instruction": "Read the report."},
                {"alias": "checker", "instruction": "Check the numbers."},
            ]
        ),
    )

    assert not answered.failed
    parent_run_id, requests = children.asked[0]
    assert parent_run_id == context.run_id
    assert requests == (
        DelegationRequest(alias="reader", instruction="Read the report."),
        DelegationRequest(alias="checker", instruction="Check the numbers."),
    )
    assert event is not None
    assert event.event_type is RunEventType.RUN_DELEGATED
    assert [entry["alias"] for entry in event.payload["children"]] == [
        "reader",
        "checker",
    ]


async def test_the_result_says_the_children_are_running_and_not_that_they_are_done() -> (
    None
):
    """The parent does not wait yet, and is told so.

    The wait is the next step of this phase. Until it exists a model that read
    "started 2" and nothing else would go looking for two answers it has not
    been given, and would invent them when it found none.
    """
    answered, _ = await answer_agent_delegate(
        Children(),
        _context("agent.delegate"),
        _call(children=[{"alias": "reader", "instruction": "Read it."}]),
    )

    assert "running now" in answered.output
    assert "do not have their results yet" in answered.output


async def test_a_refusal_from_the_creation_path_is_a_sentence_the_model_can_act_on() -> (
    None
):
    """Not an exception and not a code.

    Everything the creation path refuses is something the platform decided and
    the model could not have known: it was delegated this work itself, the
    alias is not bound, too many at once. A model handed `refused` learns
    nothing it can use; one handed the sentence can do the work itself.
    """
    children = Children(
        result=DelegationResult(
            refusal=(
                "you were delegated this work yourself, and an Agent working on "
                "somebody else's behalf cannot delegate further"
            )
        )
    )

    answered, event = await answer_agent_delegate(
        children,
        _context("agent.delegate"),
        _call(children=[{"alias": "reader", "instruction": "Read it."}]),
    )

    assert answered.failed
    assert "cannot delegate further" in answered.output
    # No event: nothing was delegated, and a timeline saying otherwise would be
    # a tree with a branch nobody grew.
    assert event is None


async def test_a_deployment_with_no_delegation_says_so_rather_than_starting_nothing() -> (
    None
):
    """`None` is "nobody wired this", which is not "you started zero children".

    The same distinction `memory.remember` and `session.search` each draw. A
    model told it started nothing would try again with different aliases
    forever.
    """
    answered, event = await answer_agent_delegate(
        None,
        _context("agent.delegate"),
        _call(children=[{"alias": "reader", "instruction": "Read it."}]),
    )

    assert answered.failed
    assert "no delegation is configured here" in answered.output
    assert event is None


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"children": []},
        {"children": "reader"},
        {"children": [{"alias": "reader"}]},
        {"children": [{"alias": "", "instruction": "Read it."}]},
        {"children": [{"alias": "reader", "instruction": "   "}]},
        {"children": [{"alias": "reader", "instruction": "Read it.", "depth": 0}]},
        {"children": [{"alias": "reader", "instruction": "Read it."}], "wait": "all"},
    ],
    ids=[
        "nothing",
        "empty",
        "not a list",
        "no instruction",
        "empty alias",
        "blank instruction",
        "unknown key on a child",
        "unknown key on the call",
    ],
)
async def test_a_call_that_is_not_a_delegation_never_reaches_the_creation_path(
    arguments: dict[str, object],
) -> None:
    """Shape is refused here, permission is refused there.

    `depth` and `wait` are in this list on purpose. Neither is an argument a
    model may pass — the first is a fact about a row and the second does not
    exist yet — and both are the shape a model would guess at if the schema
    let it. `additionalProperties: False` says so and this enforces it.
    """
    children = Children()
    answered, event = await answer_agent_delegate(
        children, _context("agent.delegate"), _call(**arguments)
    )

    assert answered.failed
    assert "invalid_arguments" in answered.output
    assert event is None
    assert children.asked == []
