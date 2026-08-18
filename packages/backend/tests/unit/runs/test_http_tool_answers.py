"""What a round does when the model calls somebody else's API.

The platform answers these itself, the way it answers `skill.load`: the call
happens outside the container, and the credential must never be inside one.

The important test in this file is the one that fails on purpose.
`test_a_write_is_refused_until_a_person_can_approve_it` pins behaviour that is
meant to be replaced — §16.3 requires an approval before an external write and
approvals arrive in the next step of the plan. Until they do, refusing is the
only correct answer, and the alternative (writing anyway, and adding approvals
later) would mean a window in which an Agent could change somebody's data with
nobody asked.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tiny_hermes.agents.domain.models import AgentSpec, DeterministicModelPolicy
from tiny_hermes.runs.application.tool_answers import answer_http_call
from tiny_hermes.runs.domain.models import RunEventType, ToolCallBlock
from tiny_hermes.runs.ports.http_calls import EgressClaim, HttpToolAnswer
from tiny_hermes.runs.ports.store import BudgetSummary, ExecutionContext
from tiny_hermes.tools.domain.http_calls import BoundOperation, HttpRequestPlan
from tiny_hermes.tools.domain.openapi import Operation, OperationParameter

SPEC = AgentSpec(
    personality="An analyst.",
    model_policy=DeterministicModelPolicy(),
)


@dataclass
class Sender:
    """Records what it was asked to send, and answers with what it was given."""

    answer: HttpToolAnswer = field(
        default_factory=lambda: HttpToolAnswer(status_code=200, body='{"orders": []}')
    )
    sent: list[tuple[HttpRequestPlan, str | None]] = field(
        default_factory=list[tuple[HttpRequestPlan, str | None]]
    )
    claims: list[EgressClaim] = field(default_factory=list[EgressClaim])

    async def send(
        self,
        plan: HttpRequestPlan,
        credential_ref: str | None,
        claim: EgressClaim,
    ) -> HttpToolAnswer:
        self.sent.append((plan, credential_ref))
        self.claims.append(claim)
        return self.answer


def bound(
    *,
    operation_id: str = "listOrders",
    method: str = "GET",
    path: str = "/orders",
    parameters: tuple[OperationParameter, ...] = (),
    body_schema: dict[str, object] | None = None,
) -> BoundOperation:
    return BoundOperation(
        tool_name="orders",
        version_id=uuid4(),
        base_url="https://api.example.com/v2",
        credential_ref="ORDERS_KEY",
        operation=Operation(
            operation_id=operation_id,
            method=method,
            path=path,
            summary=f"{method} {path}",
            parameters=parameters,
            body_schema=body_schema,
        ),
    )


def context(*operations: BoundOperation) -> ExecutionContext:
    return ExecutionContext(
        run_id=uuid4(),
        state_version=1,
        spec=SPEC,
        history=(),
        cancel_requested=False,
        pause_requested=False,
        budget=BudgetSummary(
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
        http_operations=operations,
    )


CLAIM = EgressClaim(workspace_id=uuid4(), agent_version_id=uuid4(), run_id=uuid4())


def call(name: str, **arguments: object) -> ToolCallBlock:
    return ToolCallBlock(call_id="http-1", name=name, arguments=arguments)


# -- what runs ---------------------------------------------------------------


async def test_a_bound_read_is_sent_and_the_answer_comes_back() -> None:
    sender = Sender()

    result, event = await answer_http_call(
        sender, context(bound()), call("http.orders.listOrders"), CLAIM
    )

    assert not result.failed
    assert '{"orders": []}' in result.output
    assert event is None
    plan, credential_ref = sender.sent[0]
    assert plan.url == "https://api.example.com/v2/orders"
    assert credential_ref == "ORDERS_KEY"


async def test_the_status_travels_with_the_body() -> None:
    """A model given only a body cannot tell an answer from an error document."""
    sender = Sender(HttpToolAnswer(status_code=404, body="no such order"))

    result, _ = await answer_http_call(
        sender, context(bound()), call("http.orders.listOrders"), CLAIM
    )

    assert "HTTP 404" in result.output
    assert result.failed


async def test_nothing_that_comes_back_carries_the_credential() -> None:
    sender = Sender()

    result, _ = await answer_http_call(
        sender, context(bound()), call("http.orders.listOrders"), CLAIM
    )

    assert "ORDERS_KEY" not in result.output


# -- the refusal that is meant to be replaced --------------------------------


async def test_a_write_is_refused_until_a_person_can_approve_it() -> None:
    """§16.3 wants an approval before an external write. Approvals arrive in the
    next step; refusing is the only correct behaviour until they do.

    Delete this test when the approval path lands — and not before."""
    operation = bound(
        operation_id="createOrder", method="POST", body_schema={"type": "object"}
    )
    sender = Sender()

    result, event = await answer_http_call(
        sender,
        context(operation),
        call("http.orders.createOrder", body={"sku": "abc"}),
        CLAIM,
    )

    assert result.failed
    assert "approval_required" in result.output
    assert sender.sent == []
    assert event is not None
    assert event.event_type is RunEventType.HTTP_CALL_REFUSED
    assert event.payload["reason"] == "approval_required"
    assert event.payload["operation"] == "createOrder"


async def test_the_refusal_tells_the_model_not_to_retry() -> None:
    """A model that reads "refused" as "try differently" spends the rest of its
    budget rephrasing a call that will never be allowed."""
    operation = bound(operation_id="createOrder", method="POST")

    result, _ = await answer_http_call(
        Sender(), context(operation), call("http.orders.createOrder"), CLAIM
    )

    assert "not retry" in result.output


# -- and what else is refused ------------------------------------------------


async def test_a_name_this_version_did_not_bind_is_not_authorized() -> None:
    result, event = await answer_http_call(
        Sender(), context(bound()), call("http.orders.deleteEverything"), CLAIM
    )

    assert result.failed
    assert "not_authorized" in result.output
    assert event is None


async def test_an_argument_the_operation_never_declared_is_refused() -> None:
    result, _ = await answer_http_call(
        Sender(), context(bound()), call("http.orders.listOrders", admin="true"), CLAIM
    )

    assert "invalid_arguments" in result.output


async def test_a_boundary_refusal_is_named_and_left_on_the_timeline() -> None:
    """"This workspace never approved that host" and "the API was down" are
    different facts, and a person reading the transcript needs to tell them
    apart."""
    sender = Sender(HttpToolAnswer(status_code=None, body="", refusal="host_not_allowed"))

    result, event = await answer_http_call(
        sender, context(bound()), call("http.orders.listOrders"), CLAIM
    )

    assert result.failed
    assert "host_not_allowed" in result.output
    assert event is not None
    assert event.payload["reason"] == "host_not_allowed"


async def test_a_run_with_no_outbound_face_is_told_so_rather_than_hanging() -> None:
    result, _ = await answer_http_call(
        None, context(bound()), call("http.orders.listOrders"), CLAIM
    )

    assert result.failed
    assert "outbound" in result.output


async def test_an_agent_that_bound_nothing_can_call_nothing() -> None:
    result, _ = await answer_http_call(
        Sender(), context(), call("http.orders.listOrders"), CLAIM
    )

    assert result.failed
    assert "not_authorized" in result.output


async def test_the_call_names_the_layers_it_asks_to_be_measured_against() -> None:
    """§16.5's chain includes the Agent, which is where `network.allow` lives.
    A call that named nothing would be measured against the platform alone."""
    sender = Sender()

    await answer_http_call(
        sender, context(bound()), call("http.orders.listOrders"), CLAIM
    )

    assert sender.claims == [CLAIM]


async def test_a_write_with_bad_arguments_is_still_refused_as_a_write() -> None:
    """Whether a person must approve this is a fact about the operation. Told
    to fix its arguments, a model would fix them and try again — at something
    it may not do either way."""
    operation = bound(
        operation_id="createOrder", method="POST", body_schema={"type": "object"}
    )

    result, event = await answer_http_call(
        Sender(), context(operation), call("http.orders.createOrder"), CLAIM
    )

    assert "approval_required" in result.output
    assert event is not None
