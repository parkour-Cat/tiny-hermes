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
from tiny_hermes.runs.application.tool_answers import (
    answer_agent_delegate,
    answer_artifact_read,
)
from tiny_hermes.runs.domain.models import RunEventType, ToolCallBlock, WaitPolicy
from tiny_hermes.runs.ports.artifacts import ArtifactContent
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
    outcome = await answer_agent_delegate(
        children,
        _context("shell.exec"),
        _call(children=[{"alias": "reader", "instruction": "Read it."}]),
    )

    assert outcome.result.failed
    assert "not_authorized" in outcome.result.output
    assert outcome.event is None
    assert outcome.wait is None
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

    outcome = await answer_agent_delegate(
        children,
        context,
        _call(
            children=[
                {"alias": "reader", "instruction": "Read the report."},
                {"alias": "checker", "instruction": "Check the numbers."},
            ]
        ),
    )

    assert not outcome.result.failed
    parent_run_id, requests = children.asked[0]
    assert parent_run_id == context.run_id
    assert requests == (
        DelegationRequest(alias="reader", instruction="Read the report."),
        DelegationRequest(alias="checker", instruction="Check the numbers."),
    )
    assert outcome.event is not None
    assert outcome.event.event_type is RunEventType.RUN_DELEGATED
    assert [entry["alias"] for entry in outcome.event.payload["children"]] == [
        "reader",
        "checker",
    ]
    # The wait names the children it is about, not "whatever this Run made".
    assert outcome.wait is not None
    assert outcome.wait.child_run_ids == tuple(
        child.run_id for child in children.result.children
    )
    assert outcome.wait.policy is WaitPolicy.ALL


async def test_the_result_tells_the_model_to_stop_rather_than_carry_on() -> None:
    """The round that delegated is the last round before the wait.

    Anything the model does after this call is discarded — the Run is about to
    hand back its lease and its sandbox — so the result says so. A model that
    read "started 2" and kept working would produce a turn nobody keeps and
    then be surprised by its own conversation when it woke up.
    """
    outcome = await answer_agent_delegate(
        Children(),
        _context("agent.delegate"),
        _call(children=[{"alias": "reader", "instruction": "Read it."}]),
    )

    assert "Stop working now" in outcome.result.output
    assert "woken when all of them have finished" in outcome.result.output


async def test_any_says_the_others_will_be_cancelled() -> None:
    """§13's default, in the result rather than only in the docs.

    A parent choosing `any` is choosing to have siblings killed mid-flight. It
    should read that where it makes the choice, not discover it on a bill.
    """
    outcome = await answer_agent_delegate(
        Children(),
        _context("agent.delegate"),
        _call(
            children=[{"alias": "reader", "instruction": "Read it."}], wait="any"
        ),
    )

    assert outcome.wait is not None
    assert outcome.wait.policy is WaitPolicy.ANY
    assert "the rest cancelled" in outcome.result.output


async def test_the_wait_never_outlives_the_parents_own_deadline() -> None:
    """How long to wait is the platform's answer, not the model's.

    There is no `seconds` argument, and the number comes from what is left of
    this Run's elapsed budget. Waiting past the point the parent could act on
    an answer is holding a Session head for nothing.
    """
    context = _context("agent.delegate")
    outcome = await answer_agent_delegate(
        Children(),
        context,
        _call(children=[{"alias": "reader", "instruction": "Read it."}]),
    )

    assert outcome.wait is not None
    remaining = (context.budget.elapsed_deadline_at - datetime.now(UTC)).total_seconds()
    assert outcome.wait.seconds <= remaining + 1


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

    outcome = await answer_agent_delegate(
        children,
        _context("agent.delegate"),
        _call(children=[{"alias": "reader", "instruction": "Read it."}]),
    )

    assert outcome.result.failed
    assert "cannot delegate further" in outcome.result.output
    # No event and no wait: nothing was delegated, and a parent that waited on
    # an empty set would hang until its deadline for no reason at all.
    assert outcome.event is None
    assert outcome.wait is None


async def test_a_deployment_with_no_delegation_says_so_rather_than_starting_nothing() -> (
    None
):
    """`None` is "nobody wired this", which is not "you started zero children".

    The same distinction `memory.remember` and `session.search` each draw. A
    model told it started nothing would try again with different aliases
    forever.
    """
    outcome = await answer_agent_delegate(
        None,
        _context("agent.delegate"),
        _call(children=[{"alias": "reader", "instruction": "Read it."}]),
    )

    assert outcome.result.failed
    assert "no delegation is configured here" in outcome.result.output
    assert outcome.event is None
    assert outcome.wait is None


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
        {"children": [{"alias": "reader", "instruction": "Read it."}], "depth": 0},
        {"children": [{"alias": "reader", "instruction": "Read it."}], "wait": "first"},
        {"children": [{"alias": "reader", "instruction": "Read it."}], "seconds": 30},
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
        "a wait policy that is not one",
        "a wait it does not get to time",
    ],
)
async def test_a_call_that_is_not_a_delegation_never_reaches_the_creation_path(
    arguments: dict[str, object],
) -> None:
    """Shape is refused here, permission is refused there.

    `depth` and `seconds` are in this list on purpose. Neither is an argument a
    model may pass — the first is a fact about a row, and the second is the
    platform's answer read off the parent's own budget — and both are the shape
    a model would reach for if the schema let it. `additionalProperties: False`
    says so and this enforces it.
    """
    children = Children()
    outcome = await answer_agent_delegate(
        children, _context("agent.delegate"), _call(**arguments)
    )

    assert outcome.result.failed
    assert "invalid_arguments" in outcome.result.output
    assert outcome.event is None
    assert outcome.wait is None
    assert children.asked == []


@dataclass
class Reads:
    """A stand-in artifact reader, answering as told and recording the ask."""

    answer: ArtifactContent = field(
        default_factory=lambda: ArtifactContent(
            filename="notes.txt", media_type="text/plain", size_bytes=6, text="hello!"
        )
    )
    asked: list[tuple[UUID, str]] = field(default_factory=list[tuple[UUID, str]])

    async def read(self, *, run_id: UUID, artifact_id: str) -> ArtifactContent:
        self.asked.append((run_id, artifact_id))
        return self.answer


async def test_a_file_is_read_for_the_run_that_asked_and_not_for_its_agent() -> None:
    """§13's eighth clause: a grant belongs to one piece of work.

    The id the port is handed is **this Run's**. A reader asked on behalf of an
    Agent or a Session would let a later, unrelated Run open what nobody passed
    to this one, and that is a difference no refusal downstream could recover.
    """
    reads = Reads()
    context = _context("artifact.read")
    answered = await answer_artifact_read(
        reads,
        context,
        ToolCallBlock(
            call_id="a-1", name="artifact.read", arguments={"artifact_id": "f-1"}
        ),
    )

    assert not answered.failed
    assert answered.output == "hello!"
    assert reads.asked == [(context.run_id, "f-1")]


async def test_a_file_nobody_passed_is_refused_in_words_that_reveal_nothing() -> None:
    """The refusal for "does not exist" and for "not yours" is the same one.

    Deliberately. Telling them apart would let an Agent discover which ids are
    real by reading the refusals it gets back, which is a map of somebody
    else's work drawn one guess at a time.
    """
    reads = Reads(
        answer=ArtifactContent(detail="f-9 was not passed to this piece of work")
    )
    answered = await answer_artifact_read(
        reads,
        _context("artifact.read"),
        ToolCallBlock(
            call_id="a-1", name="artifact.read", arguments={"artifact_id": "f-9"}
        ),
    )

    assert answered.failed
    assert "not passed to this piece of work" in answered.output


async def test_reading_a_file_needs_the_tool_bound_like_everything_else() -> None:
    """§10.2's second step again. Being granted a file is not being given a way
    to open one: an Agent whose Version did not bind `artifact.read` is refused
    here whether or not anybody handed it an id."""
    reads = Reads()
    answered = await answer_artifact_read(
        reads,
        _context("agent.delegate"),
        ToolCallBlock(
            call_id="a-1", name="artifact.read", arguments={"artifact_id": "f-1"}
        ),
    )

    assert answered.failed
    assert "not_authorized" in answered.output
    assert reads.asked == []


async def test_the_files_a_child_is_given_reach_the_creation_path() -> None:
    """A delegation may carry files, and they travel as ids.

    Never paths: §13's eighth clause is that there is no shared directory to
    name into. The ids arrive at the creation path untouched, which is where
    the parent's own right to pass them on is checked.
    """
    children = Children()
    outcome = await answer_agent_delegate(
        children,
        _context("agent.delegate"),
        _call(
            children=[
                {
                    "alias": "reader",
                    "instruction": "Read it.",
                    "artifacts": ["f-1", "f-2"],
                }
            ]
        ),
    )

    assert not outcome.result.failed
    _, requests = children.asked[0]
    assert requests[0].artifacts == ("f-1", "f-2")
