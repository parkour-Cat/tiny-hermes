"""A model saying it is finished, and the platform checking.

Through 0.1 the first sentence was the whole story. These tests are about the
second: an Agent that declared what "finished" means gets its claim verified in
its own sandbox before the Run is allowed to end.

The sandbox is a stand-in, as it is in `test_worker_tools.py` — what is under
test is when the Worker verifies, what it does with each answer, and what it
does when it cannot get one. Whether `bash` returns the exit code it should is
proven against a real daemon elsewhere.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.runs.ports.model import ModelResponse, StopReason
from tiny_hermes.sandbox.domain.command import CommandResult, SandboxCommand

from ..conftest import VALID_SPEC
from .test_worker_tools import Recording, StandInSandbox, drive, submit, transcript


def claims_done(said: str) -> ModelResponse:
    """A round that reports itself finished. A proposal, from here on."""
    return ModelResponse(stop_reason=StopReason.COMPLETED, text=said)


@dataclass
class ScriptedSandbox(StandInSandbox):
    """A stand-in whose commands can fail, hang, or break the daemon."""

    #: Commands whose text matches one of these fail with exit code 1.
    failing: tuple[str, ...] = ()
    #: How many of the matching runs fail before one succeeds. `None` is
    #: "every one of them".
    failures: int | None = None
    times_out: bool = False
    unavailable: bool = False
    commands: list[str] = field(default_factory=list[str])

    async def execute(
        self, *, run_id: UUID, lease_id: UUID, sandbox_id: UUID, command: SandboxCommand
    ) -> CommandResult:
        del run_id, lease_id, sandbox_id
        self.calls.append("execute")
        line = command.argv[-1]
        self.commands.append(line)
        if self.unavailable:
            raise RuntimeError("the daemon did not answer")
        if any(needle in line for needle in self.failing):
            if self.failures is None or self.failures > 0:
                if self.failures is not None:
                    self.failures -= 1
                return CommandResult(
                    exit_code=1, output="not yet", truncated=False, timed_out=self.times_out
                )
        return CommandResult(
            exit_code=0, output=self.output, truncated=False, timed_out=self.times_out
        )


@pytest.fixture
def agent_that_declares(client: TestClient, scope: dict[str, str]) -> Any:
    def build(completion: dict[str, Any] | None, *, rounds: int = 4) -> str:
        alias = f"goal-{uuid4().hex[:8]}"
        agent = client.post(
            "/api/v1/agents", headers=scope, json={"name": "Goal", "alias": alias}
        ).json()
        spec: dict[str, Any] = {
            **VALID_SPEC,
            "tools": ["shell.exec"],
            "limits": {**VALID_SPEC["limits"], "max_model_calls": rounds},
        }
        if completion is not None:
            spec["completion"] = completion
        draft = client.put(
            f"/api/v1/agents/{agent['id']}/draft",
            headers=scope,
            json={"expected_revision": 1, "spec": spec},
        ).json()
        published = client.post(
            f"/api/v1/agents/{agent['id']}/publish",
            headers=scope,
            json={"expected_revision": draft["revision"]},
        )
        assert published.status_code in (200, 201), published.text
        return str(agent["id"])

    return build


def status(client: TestClient, scope: dict[str, str], run: str) -> dict[str, Any]:
    return dict(client.get(f"/api/v1/runs/{run}", headers=scope).json())


async def test_a_claim_the_verification_contradicts_does_not_end_the_run(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    """The whole point of the slice.

    The model reports completion on its first round and keeps reporting it.
    The verification says otherwise every time, so the Run never completes —
    it works until the shared budget stops it, which is the safety valve doing
    its job rather than a bug.
    """
    agent = agent_that_declares({"verification_command": "pytest -q"}, rounds=2)
    run = submit(client, scope, agent, "write the report")
    sandbox = ScriptedSandbox(failing=("pytest -q",))

    await drive(engine, Recording(claims_done("all done")), sandbox)

    body = status(client, scope, run)
    assert body["status"] != "completed"
    assert "pytest -q" in sandbox.commands


async def test_a_verification_that_passes_lets_the_claim_stand(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    agent = agent_that_declares({"verification_command": "pytest -q"})
    run = submit(client, scope, agent, "write the report")
    sandbox = ScriptedSandbox()

    await drive(engine, Recording(claims_done("all done")), sandbox)

    assert status(client, scope, run)["status"] == "completed"
    assert "pytest -q" in sandbox.commands


async def test_a_run_that_was_wrong_once_may_still_finish(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    """`continue` is not a failure. The Run goes back to work and finishes."""
    agent = agent_that_declares({"verification_command": "pytest -q"})
    run = submit(client, scope, agent, "write the report")
    sandbox = ScriptedSandbox(failing=("pytest -q",), failures=1)

    await drive(engine, Recording(claims_done("all done"), claims_done("fixed it")), sandbox)

    assert status(client, scope, run)["status"] == "completed"
    assert sandbox.commands.count("pytest -q") == 2


async def test_the_next_round_is_told_what_did_not_pass(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    """§12.1: continue 生成下一轮指令.

    It names the failed check and does not restate the task, because the task
    is already in the conversation and the one thing the round could not know
    is why the platform disagreed with it.
    """
    agent = agent_that_declares({"verification_command": "pytest -q"})
    submit(client, scope, agent, "write the report")
    sandbox = ScriptedSandbox(failing=("pytest -q",), failures=1)
    model = Recording(claims_done("all done"), claims_done("fixed it"))

    await drive(engine, model, sandbox)

    told = [
        message
        for message in model.requests[1].messages
        if message.author == "platform"
    ]
    assert len(told) == 1
    assert "pytest -q" in told[0].text
    assert "write the report" not in told[0].text


async def test_the_platform_s_own_turn_is_not_stored_as_a_persons(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    """The transcript has to survive being read by someone else later."""
    agent = agent_that_declares({"verification_command": "pytest -q"})
    run = submit(client, scope, agent, "write the report")
    sandbox = ScriptedSandbox(failing=("pytest -q",), failures=1)

    await drive(engine, Recording(claims_done("all done"), claims_done("fixed it")), sandbox)

    rows = await transcript(engine, run)
    authored = [content for role, content in rows if role == "user" and "author" in content]
    assert len(authored) == 1
    assert "platform" in authored[0]
    assert sum(1 for role, _ in rows if role == "user") == 2


async def test_an_agent_that_declares_nothing_is_never_verified(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    """0.1's behaviour, and every Agent published before this slice."""
    agent = agent_that_declares(None)
    run = submit(client, scope, agent, "just answer")
    sandbox = ScriptedSandbox()

    await drive(engine, Recording(claims_done("done")), sandbox)

    assert status(client, scope, run)["status"] == "completed"
    assert sandbox.commands == []


async def test_a_missing_artifact_is_a_check_that_did_not_pass(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    agent = agent_that_declares({"expected_artifacts": ["report.md"]}, rounds=2)
    run = submit(client, scope, agent, "write the report")
    sandbox = ScriptedSandbox(failing=("report.md",))

    await drive(engine, Recording(claims_done("all done")), sandbox)

    assert status(client, scope, run)["status"] != "completed"
    assert any("report.md" in line for line in sandbox.commands)


async def test_an_artifact_that_exists_is_a_check_that_passed(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    agent = agent_that_declares({"expected_artifacts": ["report.md"]})
    run = submit(client, scope, agent, "write the report")
    sandbox = ScriptedSandbox()

    await drive(engine, Recording(claims_done("all done")), sandbox)

    assert status(client, scope, run)["status"] == "completed"


async def test_a_verification_that_cannot_run_pauses_for_a_person(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    """§17.3: neither accept nor reject an outcome nobody could observe.

    Calling it unmet would loop on a broken sandbox until the budget ran out;
    calling it met would accept the claim on no evidence at all.
    """
    agent = agent_that_declares({"verification_command": "pytest -q"})
    run = submit(client, scope, agent, "write the report")
    sandbox = ScriptedSandbox(unavailable=True)

    await drive(engine, Recording(claims_done("all done")), sandbox)

    body = status(client, scope, run)
    assert body["status"] == "paused"
    assert body["pause_reason"] == "operator"


async def test_a_verification_that_ran_out_of_time_is_not_a_verdict_either(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    """A command that was killed did not answer the question it was asked."""
    agent = agent_that_declares({"verification_command": "pytest -q"})
    run = submit(client, scope, agent, "write the report")
    sandbox = ScriptedSandbox(failing=("pytest -q",), times_out=True)

    await drive(engine, Recording(claims_done("all done")), sandbox)

    body = status(client, scope, run)
    assert body["status"] == "paused"
    assert body["pause_reason"] == "operator"


async def verdicts(engine: AsyncEngine, run: str) -> list[dict[str, Any]]:
    """Every judge's answer this Run has on its timeline, in order."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT payload FROM run_events WHERE run_id = :id "
                "AND event_type = 'goal_verdict' ORDER BY sequence"
            ),
            {"id": UUID(run)},
        )
        return [dict(row.payload) for row in rows.all()]


async def test_a_run_still_working_says_which_round_and_what_did_not_pass(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    """The status of a Run in its second round is the same word as in its first.

    Before this, a person watching a long Run could see it was not finished and
    nothing else — not how far along it was, and not what the platform kept
    disagreeing with. Both were already known; neither was said out loud.
    """
    agent = agent_that_declares({"verification_command": "pytest -q"}, rounds=2)
    run = submit(client, scope, agent, "write the report")

    await drive(engine, Recording(claims_done("all done"), claims_done("still done")),
                ScriptedSandbox(failing=("pytest -q",)))

    goal = status(client, scope, run)["goal"]
    assert goal["round"] == 2
    assert goal["outcome"] == "continue"
    assert goal["unmet"] == ["pytest -q"]


async def test_every_round_leaves_its_verdict_on_the_timeline(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    """The snapshot holds the latest one; the timeline holds all of them.

    A judge that answers every round but is only ever readable at the last one
    cannot explain a Run that ran out of budget four rounds after the mistake.
    """
    agent = agent_that_declares({"verification_command": "pytest -q"}, rounds=2)
    run = submit(client, scope, agent, "write the report")

    await drive(engine, Recording(claims_done("all done"), claims_done("still done")),
                ScriptedSandbox(failing=("pytest -q",)))

    recorded = await verdicts(engine, run)
    assert [entry["round"] for entry in recorded] == [1, 2]
    assert {entry["outcome"] for entry in recorded} == {"continue"}
    assert recorded[0]["unmet"] == ["pytest -q"]


async def test_a_completed_run_reports_the_verdict_that_let_it_finish(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    agent = agent_that_declares({"verification_command": "pytest -q"})
    run = submit(client, scope, agent, "write the report")

    await drive(engine, Recording(claims_done("all done")), ScriptedSandbox())

    goal = status(client, scope, run)["goal"]
    assert goal == {"round": 1, "outcome": "done", "unmet": [], "preempted": False}


async def test_the_round_a_reader_sees_is_the_round_the_model_was_given(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    """One count, not two that drift the moment a round's own call is tallied."""
    agent = agent_that_declares({"verification_command": "pytest -q"})
    run = submit(client, scope, agent, "write the report")
    model = Recording(claims_done("all done"), claims_done("fixed it"))

    await drive(engine, model, ScriptedSandbox(failing=("pytest -q",), failures=1))

    assert [request.round_index for request in model.requests] == [1, 2]
    assert [entry["round"] for entry in await verdicts(engine, run)] == [1, 2]


def calls_shell(command: str, call_id: str) -> ModelResponse:
    """A round that does a piece of the work rather than claiming to be done."""
    return ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text=f"Next: {command}",
        tool_calls=(
            ToolCallBlock(call_id=call_id, name="shell.exec", arguments={"command": command}),
        ),
    )


async def test_a_task_that_takes_several_rounds_of_work_is_ended_by_the_judge(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    """The phase exit, in one Run.

    Three rounds that each do a piece of work, then a fourth that claims to be
    finished — and the claim is checked before it is believed. What ends this
    Run is the judge accepting a verified `done`, not the model announcing one
    and not a budget running out. Every one of those rounds is on the timeline
    with what the platform decided about it.
    """
    agent = agent_that_declares({"verification_command": "pytest -q"}, rounds=6)
    run = submit(client, scope, agent, "do the three steps and then check")
    sandbox = ScriptedSandbox()
    model = Recording(
        calls_shell("./step-one", "s1"),
        calls_shell("./step-two", "s2"),
        calls_shell("./step-three", "s3"),
        claims_done("all three steps are done"),
    )

    # Driven until it settles rather than once: whether these rounds fall in one
    # slice or four is the platform's business, and the count a reader sees has
    # to survive either answer.
    for _ in range(4):
        if status(client, scope, run)["finished_at"] is not None:
            break
        await drive(engine, model, sandbox)

    body = status(client, scope, run)
    assert body["status"] == "completed"
    assert body["goal"] == {"round": 4, "outcome": "done", "unmet": [], "preempted": False}
    assert sandbox.commands[:3] == ["./step-one", "./step-two", "./step-three"]
    assert "pytest -q" in sandbox.commands

    recorded = await verdicts(engine, run)
    assert [entry["round"] for entry in recorded] == [1, 2, 3, 4]
    assert [entry["outcome"] for entry in recorded] == [
        "continue",
        "continue",
        "continue",
        "done",
    ]


async def test_a_run_that_has_not_run_yet_has_no_verdict_to_report(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, agent_that_declares: Any
) -> None:
    del engine
    agent = agent_that_declares({"verification_command": "pytest -q"})
    run = submit(client, scope, agent, "write the report")

    assert status(client, scope, run)["goal"] == {
        "round": None,
        "outcome": None,
        "unmet": [],
        "preempted": False,
    }
