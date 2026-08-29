"""摘要生成一次就落库，之后每轮读它。

`test_the_summary_is_generated_once_and_then_reused` 的判据是**模型被调了几
次**：一个每轮重新生成的实现功能上看不出区别（两轮都会拿到一份摘要，压缩都会
成功），但会让同一个 Run 重放得到不同上下文，并且每轮多付一次模型调用——这正
是产品设计 §7.4.2「摘要生成一次并持久化，之后每轮读存下来的那一份」要防的事。

这里驱动的是 Worker 的真实入口（`WorkerRuntime.run_once`，经 `drive()`），不是
一个只有测试在用的方法。触发压缩靠的是端点窗口很小（`SMALL_ENDPOINT`，与
`test_context_budget.py` 同一个配置），触发「旧会话」靠的是直接向
`session_messages` 写入早前的大段内容——不经过一次真正的 Run，因为压缩边界只
读这张表，`test_hints_are_searchable.py` 已经是这个写法的先例。
"""

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.runs.domain.summary_prompt import summary_prompt
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse, StopReason

from ..conftest import VALID_SPEC
from .test_context_budget import SMALL_ENDPOINT, ask, payloads, says, start_session, status
from .test_worker_tools import drive

#: A fixture imported and also used as a parameter name shadows itself for
#: ruff's F811 — `test_context_budget.py`'s own fixtures are only ever
#: consumed there as parameters, never imported elsewhere, so there is no
#: precedent for reusing them across files. Rebuilt here instead of imported.
CREDENTIAL = "TINY_HERMES_TEST_SUMMARY_KEY"


@pytest.fixture(autouse=True)
def credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL, "not-a-real-key")


@pytest.fixture
def small_endpoint(client: TestClient, admin_csrf: str) -> str:
    created = client.post(
        "/api/v1/model-endpoints",
        headers={"X-CSRF-Token": admin_csrf},
        json={**SMALL_ENDPOINT, "credential_ref": CREDENTIAL, "name": f"acme-{uuid4().hex[:8]}"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


@pytest.fixture
def agent_on_the_small_endpoint(
    client: TestClient, scope: dict[str, str], small_endpoint: str
) -> Any:
    def build(tools: list[str] | None = None) -> str:
        alias = f"summary-{uuid4().hex[:8]}"
        agent = client.post(
            "/api/v1/agents", headers=scope, json={"name": "Summary", "alias": alias}
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

#: `summary_prompt`'s own head text for each of its two forms — the one
#: unambiguous way to tell "the Worker asked the model to summarize" apart
#: from an ordinary round without re-parsing the whole prompt.
_FRESH_MARKER = "把下面这段对话压成一份结构化摘要"
_UPDATE_MARKER = "更新下面这份既有摘要"


def _is_summary_request(request: ModelRequest) -> bool:
    """Whether this request is the Worker asking the model to summarize.

    Structural, not incidental: a summarization call is always exactly one
    user-authored message carrying `summary_prompt`'s text, which nothing
    an ordinary round sends looks like — an ordinary round always carries at
    least the conversation plus the current turn, several messages.
    """
    if len(request.messages) != 1:
        return False
    said = request.messages[0].text
    return _FRESH_MARKER in said or _UPDATE_MARKER in said


class SummarizingRecording:
    """A model that answers ordinary rounds from a script, and separately
    counts and answers the Worker's own summarization calls.

    Two scripts rather than one: an ordinary round and a summarization call
    go through the same `ModelProvider.complete`, because this task uses the
    Run's own model endpoint (task brief, ambiguity 2) — so telling them apart
    is the spy's job, not a constructor argument the Worker does not have.
    """

    def __init__(
        self, *answers: ModelResponse, summary_text: str = "早前的对话已归纳完毕。"
    ) -> None:
        self._answers = list(answers)
        self.requests: list[ModelRequest] = []
        self.summary_requests: list[ModelRequest] = []
        self._summary_text = summary_text

    @property
    def calls(self) -> int:
        """How many times the summarizer specifically was asked."""
        return len(self.summary_requests)

    def last_prompt_contained(self, needle: str) -> bool:
        return bool(self.summary_requests) and needle in self.summary_requests[-1].messages[0].text

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if _is_summary_request(request):
            self.summary_requests.append(request)
            return ModelResponse(stop_reason=StopReason.COMPLETED, text=self._summary_text)
        return self._answers.pop(0) if self._answers else says("done")


class FailingSummarizer:
    """Ordinary rounds answer normally; the summarization call raises.

    A raised exception rather than a scripted failure response, because
    §7.4.2's ladder names timeouts and refusals in the same breath — both
    reach the Worker as "the call did not come back with anything usable",
    and an exception is the honest shape of a timeout.
    """

    def __init__(self, *answers: ModelResponse) -> None:
        self._answers = list(answers)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if _is_summary_request(request):
            raise TimeoutError("the summarizer did not answer in time")
        return self._answers.pop(0) if self._answers else says("done")


async def _seed_old_turns(
    engine: AsyncEngine,
    session_id: UUID,
    workspace_id: UUID,
    *,
    pairs: int = 2,
    size: int = 15_000,
) -> None:
    """Old conversation, written straight into `session_messages`.

    Bypasses a real Run on purpose: the compaction boundary only ever reads
    this table (`ExecutionContext.history`), so a Run that produced this
    content would cost the suite a scripted tool round for nothing it checks.
    `test_hints_are_searchable.py` inserts the same way for the same reason.

    `sessions.next_message_sequence` has to move too. `accept_run` assigns a
    new message's `sequence` from that counter, not from `MAX(sequence)` —
    skip the bump and the first real Run after this collides on `sequence=1`
    against the row this just wrote.
    """
    async with engine.begin() as connection:
        start = (
            await connection.execute(
                text(
                    "SELECT COALESCE(MAX(sequence), 0) FROM session_messages "
                    "WHERE session_id = :s"
                ),
                {"s": session_id},
            )
        ).scalar_one()
        sequence = int(start)
        for _ in range(pairs):
            for role, filler in (("user", "u"), ("assistant", "a")):
                sequence += 1
                await connection.execute(
                    text(
                        "INSERT INTO session_messages (id, session_id, workspace_id, "
                        "sequence, role, content, redacted, created_at) "
                        "VALUES (gen_random_uuid(), :s, :w, :seq, :role, :c, false, now())"
                    ),
                    {
                        "s": session_id,
                        "w": workspace_id,
                        "seq": sequence,
                        "role": role,
                        "c": json.dumps({"parts": [{"type": "text", "text": filler * size}]}),
                    },
                )
        await connection.execute(
            text(
                "UPDATE sessions SET next_message_sequence = :next "
                "WHERE id = :s AND next_message_sequence <= :seq"
            ),
            {"s": session_id, "seq": sequence, "next": sequence + 1},
        )


async def _message_count(engine: AsyncEngine, session_id: UUID) -> int:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text("SELECT count(*) FROM session_messages WHERE session_id = :s"),
                {"s": session_id},
            )
        ).scalar_one()


async def _stored_summary_row(engine: AsyncEngine, session_id: UUID) -> dict[str, Any] | None:
    """`session_compactions`, read raw rather than through `RunStore`.

    The Worker itself only ever reaches this table through
    `SqlRunStore.latest_summary`/`save_summary` — reading it back the same
    way here would only prove those two agree with each other, not that the
    Worker actually wrote and re-read through them. A raw row is the
    end-to-end check.
    """
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT first_sequence, last_sequence, summary, source, "
                    "endpoint_id, model FROM session_compactions WHERE session_id = :s"
                ),
                {"s": session_id},
            )
        ).mappings().first()
        return dict(row) if row is not None else None


async def _plant_summary_row(
    engine: AsyncEngine,
    session_id: UUID,
    workspace_id: UUID,
    *,
    first_sequence: int,
    last_sequence: int,
    text_body: str,
) -> None:
    """A `session_compactions` row written directly, not through `save_summary`.

    Only `test_a_summary_that_cuts_deeper_than_it_covers_falls_back` needs
    this — it has to control the stored *text*'s size independently of its
    recorded range, which no real compaction round can do (`_save_summary`
    always persists exactly the text the model just returned).
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO session_compactions (id, session_id, workspace_id, "
                "first_sequence, last_sequence, summary, source, endpoint_id, model, "
                "created_at) "
                "VALUES (gen_random_uuid(), :s, :w, :first, :last, :body, 'model', "
                "NULL, NULL, now())"
            ),
            {
                "s": session_id,
                "w": workspace_id,
                "first": first_sequence,
                "last": last_sequence,
                "body": text_body,
            },
        )


async def _set_ceiling(engine: AsyncEngine, workspace_id: UUID, amount: str) -> None:
    """The workspace's spending limit — `test_cost_valve.py`'s own helper,
    rebuilt here rather than imported for the same reason the endpoint
    fixtures above are: no precedent in this codebase for importing a
    private, underscore-named test helper across files."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE workspaces SET max_run_cost = :amount, cost_currency = 'USD' "
                "WHERE id = :id"
            ),
            {"amount": amount, "id": workspace_id},
        )


class RefusingSummarizer:
    """Ordinary rounds answer normally; the summarization call answers too,
    but with a non-`completed` stop reason — a provider refusal or a window
    genuinely too small for the prompt, never an exception.

    Distinct from `FailingSummarizer`: that one proves the exception path
    degrades and does not drop messages. This one proves the *other* failure
    shape — an answer that came back, just not a usable one — is visible in
    the logs the same way, not silently swallowed.
    """

    def __init__(self, *answers: ModelResponse) -> None:
        self._answers = list(answers)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if _is_summary_request(request):
            return ModelResponse(
                stop_reason=StopReason.FAILED, text="", failure="endpoint_refused"
            )
        return self._answers.pop(0) if self._answers else says("done")


async def test_the_summary_is_generated_once_and_then_reused(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    model = SummarizingRecording(says("nothing is left"), says("still nothing"))

    first_run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)
    assert status(client, scope, first_run)["status"] == "completed"

    second_run = ask(client, scope, session, "anything else?")
    await drive(engine, model, None)
    assert status(client, scope, second_run)["status"] == "completed"

    # Both rounds needed to compact — the seeded pair alone is over the small
    # endpoint's allowance, and nothing in this test ever removes it.
    first_compacted = await payloads(engine, first_run, "context_compacted")
    second_compacted = await payloads(engine, second_run, "context_compacted")
    assert len(first_compacted) == 1
    assert len(second_compacted) == 1

    assert model.calls == 1
    stored = await _stored_summary_row(engine, session_id)
    assert stored is not None
    assert stored["source"] == "model"

    # Reusing the stored summary is not the same as ignoring it: the second
    # round's own compaction record has to say `source == "model"` too, or an
    # implementation that finds the stored summary and then discards it back
    # to the structural one would pass every assertion above.
    assert second_compacted[0]["source"] == "model"

    # And the model itself has to have actually been shown that text as the
    # compacted turn — not just that a row with the right `source` exists.
    # `model.requests[-1]` is round two's own request: round two never calls
    # the summarizer (reused, not regenerated), so nothing after round one's
    # two calls (summarize, then answer) touches `summary_requests` again.
    round_two_given = model.requests[-1].messages
    assert any(message.text == stored["summary"] for message in round_two_given)


async def test_a_failed_summary_falls_back_and_does_not_drop_messages(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)
    before = await _message_count(engine, session_id)

    model = FailingSummarizer(says("nothing is left"))
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    compacted = await payloads(engine, run, "context_compacted")
    assert len(compacted) == 1
    assert compacted[0]["source"] == "structural"

    # The summarizer really was asked, and really did raise — not skipped.
    # Without this the test would also pass against today's Worker, which
    # never calls a summarizer at all and so never has one to fail.
    assert len(model.requests) == 2

    # The round still ran and still appended its own turns — a failed
    # summary degrades the context this round was planned with, never the
    # transcript. Nothing that was already there is gone, and nothing this
    # round said is missing either.
    assert await _message_count(engine, session_id) == before + 2
    assert await _stored_summary_row(engine, session_id) is None


async def test_a_later_compaction_updates_the_previous_summary(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    model = SummarizingRecording(says("nothing is left"), says("still nothing"))
    first_run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)
    assert status(client, scope, first_run)["status"] == "completed"

    first = await _stored_summary_row(engine, session_id)
    assert first is not None

    # More old content than the first summary ever saw, so the next round's
    # compaction boundary has to reach further than `first` covers.
    await _seed_old_turns(engine, session_id, workspace_id, pairs=2)

    second_run = ask(client, scope, session, "anything else?")
    await drive(engine, model, None)
    assert status(client, scope, second_run)["status"] == "completed"

    second = await _stored_summary_row(engine, session_id)
    assert second is not None
    assert second["last_sequence"] > first["last_sequence"]
    assert model.calls == 2
    assert model.last_prompt_contained(first["summary"])
    # `summary_prompt`'s own string shape (which form it opens with, that the
    # previous text lands inside it) is proven in
    # `tests/unit/runs/test_summary_prompt.py`, without a Worker or a
    # database. What only this test can prove is the wiring: that the
    # Worker actually reached for the update form on a real second
    # compaction rather than reproving the whole conversation from scratch —
    # `last_prompt_contained` above is that check; this one just pins the
    # marker string the two files must agree on.
    assert summary_prompt("x", first["summary"]).startswith(_UPDATE_MARKER)


async def test_a_summary_too_large_to_fit_falls_back_to_the_structural_plan(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    """§7.4.2 lists 窗口不足 beside 超时 and 拒绝 as a generation failure this
    round must degrade from — not a paused Run over a compaction the
    structural summary already handled.

    The model here answers, with `stop_reason == completed` and real text —
    this is not `FailingSummarizer`'s exception or `RefusingSummarizer`'s
    refusal — but the text is far bigger than what it replaced, so the
    re-plan built from it does not fit the window at all.
    """
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    model = SummarizingRecording(says("nothing is left"), summary_text="超长摘要片段" * 20_000)
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    compacted = await payloads(engine, run, "context_compacted")
    assert len(compacted) == 1
    assert compacted[0]["source"] == "structural"

    # The oversized summary was still generated and still saved — a later
    # round with more room to work with may still get to use it. Only this
    # round's own re-plan had to fall back.
    stored = await _stored_summary_row(engine, session_id)
    assert stored is not None
    assert stored["source"] == "model"


async def test_a_summary_that_cuts_deeper_than_it_covers_falls_back(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    """A stored summary can pass the numeric coverage check
    (`stored.last_sequence >= this round's boundary`) and still be unusable:
    `plan_context`'s own `through` search walks forward until the round
    fits, and a bigger summary text can push that search past the range the
    text was ever asked to explain. The resulting `CompactionRecord` would
    claim message ids and a range the summarizer never read — nothing is
    deleted from `session_messages` when that happens, but it is the
    "middle turns silently gone" bug one level up: the model is told a
    text explains turns it does not.
    """
    agent = agent_on_the_small_endpoint()

    # A throwaway Session, seeded identically, purely to learn this
    # endpoint's *structural* compaction boundary for this seed shape — the
    # exact number depends on token math this test does not want to hand-
    # encode, and the real Session below has the same shape so the same
    # number applies to it.
    probe_session = start_session(client, scope, agent)
    probe_id = UUID(probe_session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, probe_id, workspace_id, pairs=8, size=3_000)
    probe_run = ask(client, scope, probe_session, "and what is left?")
    await drive(engine, FailingSummarizer(says("nothing is left")), None)
    probe_compacted = (await payloads(engine, probe_run, "context_compacted"))[0]
    assert probe_compacted["source"] == "structural"
    baseline_last = probe_compacted["last_sequence"]

    session = start_session(client, scope, agent)
    session_id = UUID(session)
    await _seed_old_turns(engine, session_id, workspace_id, pairs=8, size=3_000)
    # Numerically sufficient (`last_sequence == baseline_last`, satisfying
    # the coverage check on its own) but with a text bigger than the terse
    # structural sentence that boundary was sized for. Calibrated against
    # `plan_context` directly (not guessed): at this seed shape and this
    # endpoint's window, this exact multiplier is the one where reusing the
    # text still *fits* the window (so this is not
    # `test_a_summary_too_large_to_fit_falls_back_to_the_structural_plan`'s
    # case) but only by `plan_context` cutting one message past
    # `baseline_last` — through 11, not 10.
    await _plant_summary_row(
        engine,
        session_id,
        workspace_id,
        first_sequence=1,
        last_sequence=baseline_last,
        text_body="占位摘要，故意写得比结构摘要长很多。" * 150,
    )

    model = SummarizingRecording(says("nothing is left"))
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    compacted = await payloads(engine, run, "context_compacted")
    assert len(compacted) == 1
    # Fell back to the structural plan rather than emitting a record that
    # claims a wider range than the planted text was ever asked to cover.
    assert compacted[0]["source"] == "structural"
    assert compacted[0]["last_sequence"] == baseline_last
    # And no model call was ever made over it — this is the reuse path
    # (the numeric check passed), not the generate path.
    assert model.calls == 0


async def test_the_summarizer_is_never_asked_once_the_cost_ceiling_is_reached(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    """§12.4 before §7.4.2. A Run already past its spending ceiling must not
    pay for a summarization call — plus whatever retries a hung endpoint
    costs — only to be stopped by the same ceiling moments later on the
    baseline plan it would have gotten anyway.

    A ceiling on an *unpriced* endpoint refuses every round outright
    (`test_cost_valve.py::test_a_ceiling_meeting_an_unpriced_endpoint_stops_the_run`)
    — the small endpoint here is never priced, so this is the cheapest
    deterministic way to force `_cost_precheck` to fail before any model
    call, without hand-computing a real cost.
    """
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)
    await _set_ceiling(engine, workspace_id, "10")

    model = SummarizingRecording(says("nothing is left"))
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    reloaded = status(client, scope, run)
    assert reloaded["status"] == "paused"
    assert reloaded["pause_reason"] == "limit"
    assert reloaded["budget"]["consumed_model_calls"] == 0
    # The one fact §12.4 alone cannot prove: not just that no round ran, but
    # that the summarizer specifically was never reached for — a summary
    # call costs money on the real endpoint even though nothing here counts
    # it against `consumed_model_calls`, which is exactly why it must never
    # happen on a Run already refused.
    assert model.calls == 0
    assert model.requests == []
    assert await _stored_summary_row(engine, session_id) is None


async def test_a_refused_summary_is_logged_same_as_a_timeout(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`FailingSummarizer`'s exception already logs
    (`logger.exception` in `_generate_summary`). An answer that came back
    with a non-`completed` stop reason — a provider refusal, or a window
    genuinely too small — is a different failure shape and must be exactly
    as visible, not silently swallowed as a bare `None`.
    """
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    model = RefusingSummarizer(says("nothing is left"))
    run = ask(client, scope, session, "and what is left?")
    with caplog.at_level("WARNING", logger="tiny_hermes.runs.application.worker"):
        await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    compacted = await payloads(engine, run, "context_compacted")
    assert compacted[0]["source"] == "structural"
    assert any(
        record.levelname in ("WARNING", "ERROR")
        and "summary" in record.getMessage().lower()
        for record in caplog.records
    )


async def test_a_saved_summary_records_which_model_wrote_it(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    """A summary is written once and reused — every Session summarized
    under this row keeps whatever `model` was recorded here forever. Leaving
    it `None` is a hole nothing can backfill later except by guessing, so
    this has to be the real model string the endpoint declared, not merely
    "some value or other"."""
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    model = SummarizingRecording(says("nothing is left"))
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    stored = await _stored_summary_row(engine, session_id)
    assert stored is not None
    assert stored["endpoint_id"] is not None
    assert stored["model"] == SMALL_ENDPOINT["model"]

