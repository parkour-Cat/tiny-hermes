"""What a round does when the model calls somebody else's API.

The platform answers these itself, the way it answers `skill.load`: the call
happens outside the container, and the credential must never be inside one.

This file used to say a write was refused because approvals did not exist. They
do now, so it says something different, and the difference is what §2 of the
plan delivered. A write runs under the policy its Version chose at publish —
`disabled` refuses it forever, `preauthorized` runs it because a workspace
administrator already approved this narrow scope, and `governance` stops the
Run until somebody decides.

The shape of "stops" is worth reading twice. The round produces **no tool
result**: the Run's turns are discarded, and when it resumes the model is asked
from the same history it had. A result saying "you are waiting" would sit in
the conversation forever, and the model would have to be trusted to reissue the
call after being told it had already made it.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from tiny_hermes.agents.domain.models import (
    AgentSpec,
    DeterministicModelPolicy,
    HttpToolBinding,
    WritePolicy,
)
from tiny_hermes.runs.application.tool_answers import answer_http_call
from tiny_hermes.runs.domain.approval import ApprovalType, NormalizedCall
from tiny_hermes.runs.domain.models import CallerType, RunEventType, ToolCallBlock
from tiny_hermes.runs.ports.approvals import ApprovalCheck, ApprovalVerdict
from tiny_hermes.runs.ports.http_calls import EgressClaim, HttpToolAnswer
from tiny_hermes.runs.ports.store import BudgetSummary, ExecutionContext
from tiny_hermes.tools.domain.http_calls import BoundOperation, HttpRequestPlan
from tiny_hermes.tools.domain.openapi import Operation, OperationParameter

VERSION_ID = uuid4()
CLAIM = EgressClaim(workspace_id=uuid4(), agent_version_id=uuid4(), run_id=uuid4())


def spec(policy: WritePolicy | None) -> AgentSpec:
    return AgentSpec(
        personality="An analyst.",
        model_policy=DeterministicModelPolicy(),
        http_tools=(
            HttpToolBinding(
                http_tool_version_id=VERSION_ID,
                operations=("listOrders", "createOrder"),
                write_policy=policy,
            ),
        ),
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


@dataclass
class Gate:
    """The approvals, as a round sees them, and what it asked about."""

    verdict: ApprovalVerdict = ApprovalVerdict.REQUESTED
    asked: list[NormalizedCall] = field(default_factory=list[NormalizedCall])
    types: list[ApprovalType] = field(default_factory=list[ApprovalType])
    permissions: list[str | None] = field(default_factory=list[str | None])

    async def check(
        self,
        *,
        run_id: UUID,
        approval_type: ApprovalType,
        tool: str,
        call_id: str,
        call: NormalizedCall,
        required_permission: str | None,
    ) -> ApprovalCheck:
        del run_id, tool, call_id
        self.asked.append(call)
        self.types.append(approval_type)
        self.permissions.append(required_permission)
        return ApprovalCheck(self.verdict, uuid4())


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
        version_id=VERSION_ID,
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


def writer() -> BoundOperation:
    return bound(operation_id="createOrder", method="POST")


def context(
    *operations: BoundOperation,
    policy: WritePolicy | None = None,
    caller_type: CallerType | None = None,
) -> ExecutionContext:
    return ExecutionContext(
        run_id=uuid4(),
        state_version=1,
        spec=spec(policy),
        history=(),
        cancel_requested=False,
        pause_requested=False,
        caller_type=caller_type,
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


def call(name: str, **arguments: object) -> ToolCallBlock:
    return ToolCallBlock(call_id="http-1", name=name, arguments=arguments)


# -- reads -------------------------------------------------------------------


async def test_a_bound_read_is_sent_and_the_answer_comes_back() -> None:
    sender = Sender()

    outcome = await answer_http_call(
        sender, context(bound()), call("http.orders.listOrders"), CLAIM
    )

    assert outcome.result is not None
    assert not outcome.result.failed
    assert '{"orders": []}' in outcome.result.output
    plan, credential_ref = sender.sent[0]
    assert plan.url == "https://api.example.com/v2/orders"
    assert credential_ref == "ORDERS_KEY"


async def test_a_read_never_asks_anybody() -> None:
    """§16.3's approvals are about changing things. A read that stopped for a
    person would make every dashboard an administrator's queue."""
    gate = Gate()

    await answer_http_call(
        Sender(), context(bound()), call("http.orders.listOrders"), CLAIM, gate
    )

    assert gate.asked == []


async def test_the_status_travels_with_the_body() -> None:
    """A model given only a body cannot tell an answer from an error document."""
    sender = Sender(HttpToolAnswer(status_code=404, body="no such order"))

    outcome = await answer_http_call(
        sender, context(bound()), call("http.orders.listOrders"), CLAIM
    )

    assert outcome.result is not None
    assert "HTTP 404" in outcome.result.output
    assert outcome.result.failed


async def test_nothing_that_comes_back_carries_the_credential() -> None:
    outcome = await answer_http_call(
        Sender(), context(bound()), call("http.orders.listOrders"), CLAIM
    )

    assert outcome.result is not None
    assert "ORDERS_KEY" not in outcome.result.output


async def test_the_call_names_the_layers_it_asks_to_be_measured_against() -> None:
    """§16.5's chain includes the Agent, which is where `network.allow` lives.
    A call that named nothing would be measured against the platform alone."""
    sender = Sender()

    await answer_http_call(
        sender, context(bound()), call("http.orders.listOrders"), CLAIM
    )

    assert sender.claims == [CLAIM]


# -- writes, under the policy the Version chose ------------------------------


async def test_a_governance_write_stops_the_run_and_produces_no_result() -> None:
    """The Run waits. Appending a result saying so would leave the model told
    it had made a call it has not made."""
    sender = Sender()

    outcome = await answer_http_call(
        sender,
        context(writer(), policy=WritePolicy.GOVERNANCE),
        call("http.orders.createOrder"),
        CLAIM,
        Gate(ApprovalVerdict.REQUESTED),
    )

    assert outcome.result is None
    assert outcome.approval is not None
    assert outcome.approval.verdict is ApprovalVerdict.REQUESTED
    assert sender.sent == []


async def test_an_external_write_is_asked_of_an_administrator() -> None:
    """§16.3 lists "sending an external message" and "creating an order" under
    governance, not under what an end user may confirm about their own data."""
    gate = Gate()

    await answer_http_call(
        Sender(),
        context(writer(), policy=WritePolicy.GOVERNANCE),
        call("http.orders.createOrder"),
        CLAIM,
        gate,
    )

    assert gate.types == [ApprovalType.GOVERNANCE_APPROVAL]
    assert gate.permissions == ["http.orders.write"]


async def test_an_end_users_own_write_opens_a_user_confirmation_instead() -> None:
    """End-user entry design §5: the same `governance` policy asks a
    different person depending on whose Run this is. A `caller_type=
    end_user` Run is that end user's own conversation, so the write it
    triggers is their own write — answered by them, not by a workspace
    administrator standing in for them."""
    gate = Gate()

    await answer_http_call(
        Sender(),
        context(writer(), policy=WritePolicy.GOVERNANCE, caller_type=CallerType.END_USER),
        call("http.orders.createOrder"),
        CLAIM,
        gate,
    )

    assert gate.types == [ApprovalType.USER_CONFIRMATION]


async def test_the_approval_is_bound_to_the_composed_request() -> None:
    """A person is shown the URL that would actually be requested, not the
    arguments before this platform decided what to do with them."""
    gate = Gate()

    await answer_http_call(
        Sender(),
        context(writer(), policy=WritePolicy.GOVERNANCE),
        call("http.orders.createOrder"),
        CLAIM,
        gate,
    )

    assert gate.asked[0].document["target"] == "https://api.example.com/v2/orders"


async def test_an_approved_write_runs() -> None:
    sender = Sender()

    outcome = await answer_http_call(
        sender,
        context(writer(), policy=WritePolicy.GOVERNANCE),
        call("http.orders.createOrder"),
        CLAIM,
        Gate(ApprovalVerdict.APPROVED),
    )

    assert outcome.result is not None
    assert not outcome.result.failed
    assert [plan.method for plan, _ in sender.sent] == ["POST"]


async def test_a_run_already_waiting_does_not_ask_twice() -> None:
    outcome = await answer_http_call(
        Sender(),
        context(writer(), policy=WritePolicy.GOVERNANCE),
        call("http.orders.createOrder"),
        CLAIM,
        Gate(ApprovalVerdict.PENDING),
    )

    assert outcome.result is None
    assert outcome.approval is not None
    assert outcome.approval.verdict is ApprovalVerdict.PENDING


async def test_a_preauthorized_write_runs_without_asking() -> None:
    """A workspace administrator approved this narrow scope when the version
    was published; that is what the choice means."""
    sender = Sender()
    gate = Gate()

    outcome = await answer_http_call(
        sender,
        context(writer(), policy=WritePolicy.PREAUTHORIZED),
        call("http.orders.createOrder"),
        CLAIM,
        gate,
    )

    assert outcome.result is not None
    assert gate.asked == []
    assert len(sender.sent) == 1


async def test_a_disabled_write_is_refused_and_never_asked_about() -> None:
    sender = Sender()
    gate = Gate()

    outcome = await answer_http_call(
        sender,
        context(writer(), policy=WritePolicy.DISABLED),
        call("http.orders.createOrder"),
        CLAIM,
        gate,
    )

    assert outcome.result is not None
    assert "write_disabled" in outcome.result.output
    assert "not retry" in outcome.result.output
    assert gate.asked == []
    assert sender.sent == []
    assert outcome.event is not None
    assert outcome.event.event_type is RunEventType.HTTP_CALL_REFUSED


async def test_a_binding_with_no_policy_is_read_as_disabled() -> None:
    """Publishing refuses that combination, so reaching it means a version
    published before the check existed — and refusing is the safe reading of
    silence."""
    outcome = await answer_http_call(
        Sender(), context(writer()), call("http.orders.createOrder"), CLAIM, Gate()
    )

    assert outcome.result is not None
    assert "write_disabled" in outcome.result.output


async def test_a_platform_with_no_gate_refuses_rather_than_writes() -> None:
    sender = Sender()

    outcome = await answer_http_call(
        sender,
        context(writer(), policy=WritePolicy.GOVERNANCE),
        call("http.orders.createOrder"),
        CLAIM,
    )

    assert outcome.result is not None
    assert outcome.result.failed
    assert sender.sent == []


# -- and what is refused before anybody is asked -----------------------------


async def test_a_name_this_version_did_not_bind_is_not_authorized() -> None:
    outcome = await answer_http_call(
        Sender(), context(bound()), call("http.orders.deleteEverything"), CLAIM
    )

    assert outcome.result is not None
    assert "not_authorized" in outcome.result.output
    assert outcome.approval is None


async def test_an_argument_the_operation_never_declared_is_refused() -> None:
    outcome = await answer_http_call(
        Sender(), context(bound()), call("http.orders.listOrders", admin="true"), CLAIM
    )

    assert outcome.result is not None
    assert "invalid_arguments" in outcome.result.output


async def test_a_malformed_write_is_refused_before_anybody_is_asked() -> None:
    """An approval document carrying a parameter the operation never declared
    would describe a request this platform would refuse to make anyway."""
    gate = Gate()

    outcome = await answer_http_call(
        Sender(),
        context(writer(), policy=WritePolicy.GOVERNANCE),
        call("http.orders.createOrder", admin="true"),
        CLAIM,
        gate,
    )

    assert outcome.result is not None
    assert "invalid_arguments" in outcome.result.output
    assert gate.asked == []


async def test_a_boundary_refusal_is_named_and_left_on_the_timeline() -> None:
    """"This workspace never approved that host" and "the API was down" are
    different facts, and a person reading the transcript needs to tell them
    apart."""
    sender = Sender(
        HttpToolAnswer(status_code=None, body="", refusal="host_not_allowed")
    )

    outcome = await answer_http_call(
        sender, context(bound()), call("http.orders.listOrders"), CLAIM
    )

    assert outcome.result is not None
    assert outcome.result.failed
    assert "host_not_allowed" in outcome.result.output
    assert outcome.event is not None
    assert outcome.event.payload["reason"] == "host_not_allowed"


async def test_a_run_with_no_outbound_face_is_told_so_rather_than_hanging() -> None:
    outcome = await answer_http_call(
        None, context(bound()), call("http.orders.listOrders"), CLAIM
    )

    assert outcome.result is not None
    assert outcome.result.failed
    assert "outbound" in outcome.result.output


async def test_an_agent_that_bound_nothing_can_call_nothing() -> None:
    outcome = await answer_http_call(
        Sender(), context(), call("http.orders.listOrders"), CLAIM
    )

    assert outcome.result is not None
    assert outcome.result.failed
    assert "not_authorized" in outcome.result.output


@pytest.mark.parametrize("verdict", list(ApprovalVerdict))
async def test_only_an_approved_answer_lets_the_write_through(
    verdict: ApprovalVerdict,
) -> None:
    """One of the four proceeds. The parametrization is what keeps a fifth from
    quietly defaulting to "go ahead"."""
    sender = Sender()

    outcome = await answer_http_call(
        sender,
        context(writer(), policy=WritePolicy.GOVERNANCE),
        call("http.orders.createOrder"),
        CLAIM,
        Gate(verdict),
    )

    proceeded = outcome.result is not None
    assert proceeded is (verdict is ApprovalVerdict.APPROVED)
