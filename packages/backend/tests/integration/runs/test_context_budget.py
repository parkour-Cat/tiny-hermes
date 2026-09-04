"""What the Worker sends when the conversation outgrows the window.

The planner is proven as a pure function in `tests/unit/runs/test_context_budget.py`.
What is proven here is the wiring §7.4.2 depends on: the window comes off a real
endpoint row, the trimming and the compaction leave events a person can read,
the originals stay in the transcript whatever the model was shown, and the round
that cannot be made to fit is not sent at all.

The model is a stand-in, as everywhere else in this directory — the point is
what the platform decided to give it, not what it said back.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.runs.ports.model import ModelResponse, StopReason

from ..conftest import VALID_SPEC
from .test_worker_tools import Recording, StandInSandbox, drive, transcript

CREDENTIAL = "TINY_HERMES_TEST_BUDGET_KEY"

#: 13,568 minus the 4,096 it reserves for output leaves 9,472 — exactly the sum
#: of the default segment targets, so this endpoint is the smallest one an Agent
#: can still be published against. Anything a Run then says has to be planned
#: for, which is what makes these tests short enough to read.
SMALL_ENDPOINT: dict[str, Any] = {
    "name": "acme-small",
    "kind": "openai_compatible",
    "base_url": "https://models.example.com/v1",
    "model": "acme-small",
    "context_window": 13_568,
    "max_output_tokens": 4_096,
    "usage_quality": "provider",
    "credential_ref": CREDENTIAL,
}

ALLOWANCE = 9_472


@pytest.fixture(autouse=True)
def credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL, "not-a-real-key")


@pytest.fixture
def small_endpoint(client: TestClient, admin_csrf: str) -> str:
    created = client.post(
        "/api/v1/model-endpoints",
        headers={"X-CSRF-Token": admin_csrf},
        json={**SMALL_ENDPOINT, "name": f"acme-{uuid4().hex[:8]}"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


@pytest.fixture
def agent_on_the_small_endpoint(
    client: TestClient, scope: dict[str, str], small_endpoint: str
) -> Any:
    def build(tools: list[str] | None = None) -> str:
        alias = f"budget-{uuid4().hex[:8]}"
        agent = client.post(
            "/api/v1/agents", headers=scope, json={"name": "Budget", "alias": alias}
        ).json()
        spec = {
            **VALID_SPEC,
            "tools": tools or [],
            "model_policy": {
                "provider": "openai_compatible",
                "endpoint_id": small_endpoint,
            },
        }
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
        assert published.status_code == 201, published.text
        return str(agent["id"])

    return build


def start_session(client: TestClient, scope: dict[str, str], agent_id: str) -> str:
    return str(
        client.post(
            "/api/v1/sessions", headers=scope, json={"agent_id": agent_id}
        ).json()["id"]
    )


def ask(client: TestClient, scope: dict[str, str], session_id: str, said: str) -> str:
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": uuid4().hex},
        json={"session_id": session_id, "input": said},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def status(client: TestClient, scope: dict[str, str], run: str) -> dict[str, Any]:
    return dict(client.get(f"/api/v1/runs/{run}", headers=scope).json())


async def payloads(engine: AsyncEngine, run: str, event_type: str) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT payload FROM run_events WHERE run_id = :id "
                "AND event_type = :kind ORDER BY sequence"
            ),
            {"id": UUID(run), "kind": event_type},
        )
        return [dict(row.payload) for row in rows.all()]


def says(text_said: str) -> ModelResponse:
    return ModelResponse(stop_reason=StopReason.COMPLETED, text=text_said)


def calls_shell(command: str, call_id: str, said: str = "") -> ModelResponse:
    return ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text=said,
        tool_calls=(
            ToolCallBlock(call_id=call_id, name="shell.exec", arguments={"command": command}),
        ),
    )


def fails() -> ModelResponse:
    """A round the model refused. Used here to script the Worker's own
    auxiliary summarization call into `§7.4.2`'s first failure rung, so a
    `Recording` scripted for one ordinary round is not silently consumed by
    it — see `test_compaction_summary.py` for that call's own behaviour."""
    return ModelResponse(stop_reason=StopReason.FAILED, text="", failure="scripted_refusal")


async def test_a_tool_result_too_large_to_carry_is_trimmed_and_recorded(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    """Step one of the fixed order, driven end to end.

    The command really did produce 40,000 characters, and the next round cannot
    be given them. What it is given instead still answers the call that asked.
    """
    agent = agent_on_the_small_endpoint(["shell.exec"])
    session = start_session(client, scope, agent)
    run = ask(client, scope, session, "run the suite")
    model = Recording(calls_shell("./suite", "c1"), says("the suite passed"))

    await drive(engine, model, StandInSandbox(output="x" * 40_000))

    assert status(client, scope, run)["status"] == "completed"
    trimmed = await payloads(engine, run, "context_trimmed")
    assert [record["segment"] for record in trimmed] == ["old_tool_results"]
    assert trimmed[0]["references"] == ["c1"]
    assert trimmed[0]["freed_estimate"] > 0

    # What the second round was actually given: the same call, answered, with a
    # stub that says where the real output is.
    second = model.requests[1].messages[-1]
    assert second.role == "tool"
    block = second.blocks[0]
    assert "c1" in getattr(block, "output", "")
    assert "40000" in getattr(block, "output", "")

    # And the transcript still holds every character of it. Nothing the planner
    # does is a deletion.
    rows = await transcript(engine, run)
    assert any("x" * 40_000 in content for _, content in rows)


async def test_an_old_conversation_is_compacted_with_its_range_and_ids(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    """Step four, on a persistent Session that outgrew the window.

    Two long turns from an earlier Run, then a new question. Nothing in the
    older turns is a tool result, so trimming has nothing to give and the
    compaction is what makes the round fit — and it leaves behind exactly what
    §7.4.2 asks it to: the range it covered and the ids it stood in for.
    """
    agent = agent_on_the_small_endpoint(["shell.exec"])
    session = start_session(client, scope, agent)
    ask(client, scope, session, "start the report")
    first = Recording(
        calls_shell("./gather", "c1", said="A" * 15_000),
        says("B" * 15_000),
    )
    await drive(engine, first, StandInSandbox(output="ok\n"))

    second = ask(client, scope, session, "and what is left?")
    # This Session has no stored summary yet, so the Worker also asks this
    # same model to write one before it asks the round's own question — see
    # `test_compaction_summary.py` for that call. Scripted to fail here: this
    # test is about the *structural* shape §7.4.2 requires either way, and a
    # scripted failure is the one answer that reliably lands on it rather
    # than on whatever the summarizer happened to be given.
    model = Recording(fails(), says("nothing is left"))
    await drive(engine, model, StandInSandbox(output="ok\n"))

    assert status(client, scope, second)["status"] == "completed"
    compacted = await payloads(engine, second, "context_compacted")
    assert len(compacted) == 1
    assert compacted[0]["source"] == "structural"
    assert compacted[0]["first_sequence"] == 1
    assert compacted[0]["covered"] == len(compacted[0]["message_ids"])
    assert compacted[0]["covered"] >= 2

    # The summary stands where the covered turns did, and the question the Run
    # was actually asked is still the last thing the model sees, whole. The
    # round's own request is the *last* one this model saw — the first was
    # the summarization attempt that failed.
    given = model.requests[-1].messages
    assert "compacted by the platform" in given[0].text
    assert given[-1].text == "and what is left?"
    assert len(given) == 5 - compacted[0]["covered"] + 1


async def test_a_round_that_cannot_be_made_to_fit_is_never_sent(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    """§7.4.2's last line, and the runtime twin of the publish refusal.

    The request alone is over the allowance, and a request is not something the
    platform is allowed to shorten. So there is no provider call to make: the
    Run pauses for a person, with every message it ever had still on it.
    """
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    run = ask(client, scope, session, "summarize this: " + "y" * 32_000)
    model = Recording(says("never asked"))

    await drive(engine, model, None)

    body = status(client, scope, run)
    assert body["status"] == "paused"
    assert body["pause_reason"] == "context_overflow"
    assert model.requests == []
    assert body["budget"]["consumed_model_calls"] == 0
    rows = await transcript(engine, run)
    assert any("y" * 32_000 in content for _, content in rows)


async def test_a_context_overflow_pause_can_be_recovered_and_keeps_what_it_spent(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    """The release gate's rule for this pause: it recovers, and recovery
    resets nothing.

    `paused(context_overflow)` is the one pause reached without spending a
    model call — the request was never sent. That makes it the awkward case for
    "recovery does not reset a counter", because the counter it must not reset
    is one that has not moved. Asserted anyway, and deliberately: the rule is
    about the platform never starting a Run's accounting again, not about the
    number happening to be interesting.

    What makes it recoverable is a person acting — shortening the input or
    moving the Agent to a larger endpoint. Here the conversation is redacted to
    something that fits, which is the same shape as the first: the Run resumes
    against a history it can send.
    """
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    run = ask(client, scope, session, "summarize this: " + "y" * 32_000)
    await drive(engine, Recording(says("never asked")), None)

    stopped = status(client, scope, run)
    assert stopped["pause_reason"] == "context_overflow"
    spent_before = stopped["budget"]["consumed_model_calls"]
    # Resume is offered: this pause is somebody's to clear, not a dead end.
    assert "resume" in stopped["available_actions"]

    # The person's half. Nothing the platform is allowed to do on its own —
    # §7.4.2 forbids shortening the request — so the oversized turn is dropped
    # by hand, which is what redaction is for.
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE session_messages SET redacted = true "
                "WHERE session_id = :s AND content::text LIKE :big"
            ),
            {"s": UUID(session), "big": "%" + "y" * 200 + "%"},
        )
    later = ask(client, scope, session, "who are you?")
    assert later

    resumed = client.post(
        f"/api/v1/runs/{run}/resume",
        headers=scope,
        json={"expected_state_version": stopped["state_version"]},
    )
    assert resumed.status_code == 200, resumed.text

    await drive(engine, Recording(says("an agent")), None)

    reloaded = status(client, scope, run)
    assert reloaded["status"] == "completed"
    # It ran, and its accounting continued from where the pause left it rather
    # than starting again.
    assert reloaded["budget"]["consumed_model_calls"] >= spent_before
    assert reloaded["budget"]["consumed_model_calls"] > 0


async def test_a_conversation_inside_the_window_is_sent_as_it_stands(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    """The ordinary Run, which is most of them.

    Nothing is trimmed, nothing is compacted, and the timeline says nothing
    happened to the context — because nothing did.
    """
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    run = ask(client, scope, session, "who are you?")
    model = Recording(says("an agent"))

    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    assert await payloads(engine, run, "context_trimmed") == []
    assert await payloads(engine, run, "context_compacted") == []
    assert [message.text for message in model.requests[0].messages] == ["who are you?"]
    assert ALLOWANCE == SMALL_ENDPOINT["context_window"] - SMALL_ENDPOINT[
        "max_output_tokens"
    ]


async def test_a_context_overflow_pause_is_recovered_by_widening_the_window(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    admin_csrf: str,
    small_endpoint: str,
    agent_on_the_small_endpoint: Any,
) -> None:
    """The other way out, and the one that needs no editing of the
    conversation: a platform administrator widens the endpoint's window with
    `PATCH /api/v1/model-endpoints/{id}`, and the paused Run — fixed to a
    Version that names this endpoint — reads the new window at its first round
    after the resume. It keeps every message it had and the round that could
    not be sent is sent. Until this route existed, only a database session
    could do this."""
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    run = ask(client, scope, session, "summarize this: " + "y" * 32_000)
    await drive(engine, Recording(says("never asked")), None)
    paused = status(client, scope, run)
    assert paused["pause_reason"] == "context_overflow"

    widened = client.patch(
        f"/api/v1/model-endpoints/{small_endpoint}",
        headers={"X-CSRF-Token": admin_csrf},
        json={"context_window": 200_000},
    )
    assert widened.status_code == 200, widened.text
    resumed = client.post(
        f"/api/v1/runs/{run}/resume",
        headers=scope,
        json={"expected_state_version": paused["state_version"]},
    )
    assert resumed.status_code == 200, resumed.text

    model = Recording(says("summarized"))
    await drive(engine, model, None)

    body = status(client, scope, run)
    assert body["status"] == "completed"
    assert len(model.requests) == 1
    assert body["budget"]["consumed_model_calls"] == 1
    assert any("y" * 32_000 in content for _, content in await transcript(engine, run))
