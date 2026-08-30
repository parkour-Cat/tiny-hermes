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
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.agents.domain.models import EndpointModelPolicy
from tiny_hermes.model_catalog.domain.pricing import TokenPrices, cost_of
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.runs.domain.summary_prompt import summary_prompt
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse, StopReason

from ..conftest import VALID_SPEC
from .test_context_budget import SMALL_ENDPOINT, ask, payloads, says, start_session, status
from .test_worker_tools import StandInSandbox, drive

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
def summary_endpoint(client: TestClient, admin_csrf: str) -> str:
    """A second endpoint, distinct from `small_endpoint`, for the tests that
    declare `summary_endpoint_id` and need to tell "the call went to the
    Agent's own endpoint" apart from "the call went to the summary one" by
    id rather than by size — same `context_window` as `SMALL_ENDPOINT` so it
    never changes when compaction itself triggers, which is decided against
    the *main* endpoint's window (`ContextBudget`/`ContextWindow`), not this
    one's."""
    created = client.post(
        "/api/v1/model-endpoints",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            **SMALL_ENDPOINT,
            "credential_ref": CREDENTIAL,
            "name": f"acme-summary-{uuid4().hex[:8]}",
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


@pytest.fixture
def agent_on_the_small_endpoint(
    client: TestClient, scope: dict[str, str], small_endpoint: str
) -> Any:
    def build(
        tools: list[str] | None = None,
        summary_endpoint_id: str | None = None,
        max_model_calls: int | None = None,
    ) -> str:
        alias = f"summary-{uuid4().hex[:8]}"
        agent = client.post(
            "/api/v1/agents", headers=scope, json={"name": "Summary", "alias": alias}
        ).json()
        policy: dict[str, object] = {
            "provider": "openai_compatible",
            "endpoint_id": small_endpoint,
        }
        if summary_endpoint_id is not None:
            policy["summary_endpoint_id"] = summary_endpoint_id
        spec: dict[str, Any] = {
            **VALID_SPEC,
            "tools": tools or [],
            "model_policy": policy,
        }
        if max_model_calls is not None:
            spec["limits"] = {**VALID_SPEC["limits"], "max_model_calls": max_model_calls}
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


def _endpoint_of(request: ModelRequest) -> UUID | None:
    """Which endpoint this request would actually be dispatched to.

    `drive()` hands the spy to `WorkerRuntime` as `model=`, standing directly
    in for `ModelProvider` — the same seam a real `ModelRouter.complete`
    reads `request.policy.endpoint_id` from to pick which endpoint answers
    (`model_router.py`). Reading `request.policy` here is that boundary, not
    a value read back from `_summary_policy` before the call was ever made —
    the distinction the routing tests below exist to keep.
    """
    policy = request.policy
    return policy.endpoint_id if isinstance(policy, EndpointModelPolicy) else None


class SummarizingRecording:
    """A model that answers ordinary rounds from a script, and separately
    counts and answers the Worker's own summarization calls.

    Two scripts rather than one: an ordinary round and a summarization call
    go through the same `ModelProvider.complete`, because this task uses the
    Run's own model endpoint (task brief, ambiguity 2) — so telling them apart
    is the spy's job, not a constructor argument the Worker does not have.
    """

    def __init__(
        self,
        *answers: ModelResponse,
        summary_text: str = "早前的对话已归纳完毕。",
        # `None` by default rather than some fixed number: the billing tests
        # are the only ones that need the summarizer to have reported usage
        # at all, and every other test in this file relies on the old,
        # usage-less shape to keep its own assertions (about `source`,
        # about which text a later round was shown) unaffected by a second
        # concern this class did not use to have.
        summary_input_tokens: int | None = None,
        summary_output_tokens: int | None = None,
    ) -> None:
        self._answers = list(answers)
        self.requests: list[ModelRequest] = []
        self.summary_requests: list[ModelRequest] = []
        self._summary_text = summary_text
        self._summary_input_tokens = summary_input_tokens
        self._summary_output_tokens = summary_output_tokens

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
            return ModelResponse(
                stop_reason=StopReason.COMPLETED,
                text=self._summary_text,
                input_tokens=self._summary_input_tokens,
                output_tokens=self._summary_output_tokens,
            )
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
                    "SELECT first_sequence, last_sequence, summary, "
                    "endpoint_id, model FROM session_compactions WHERE session_id = :s"
                ),
                {"s": session_id},
            )
        ).mappings().first()
        return dict(row) if row is not None else None


async def _budget_row(engine: AsyncEngine, root_run_id: UUID) -> dict[str, Any] | None:
    """`run_budget_scopes`, read raw — the same reason `_stored_summary_row`
    is: `SqlRunStore` reading its own write back would only prove the two
    agree with each other, not that a summarization call's usage and cost
    actually landed on the row `_cost_precheck` reads from next round.

    Keyed on `root_run_id`, not `run_id`: for a Head Run created directly
    (every Run in this file) the two are the same value, but the column that
    actually carries the shared total is `root_run_id` (see the comment near
    `RunBudgetScopeRow` in `sql_store.py`), and reading by the wrong column
    would silently pass even if a future change billed the wrong scope.
    """
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT consumed_cost, consumed_tokens, cost_currency, "
                    "cost_quality FROM run_budget_scopes WHERE root_run_id = :id"
                ),
                {"id": root_run_id},
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
                "first_sequence, last_sequence, summary, endpoint_id, model, "
                "created_at) "
                "VALUES (gen_random_uuid(), :s, :w, :first, :last, :body, "
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

    def __init__(
        self,
        *answers: ModelResponse,
        summary_input_tokens: int | None = None,
        summary_output_tokens: int | None = None,
    ) -> None:
        self._answers = list(answers)
        self.requests: list[ModelRequest] = []
        self._summary_input_tokens = summary_input_tokens
        self._summary_output_tokens = summary_output_tokens

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if _is_summary_request(request):
            return ModelResponse(
                stop_reason=StopReason.FAILED,
                text="",
                failure="endpoint_refused",
                input_tokens=self._summary_input_tokens,
                output_tokens=self._summary_output_tokens,
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


#: The answer the model gives in the oversized case below, named so the row
#: read back afterwards can be checked against it by value rather than by the
#: `source` column that used to stand in for "a model wrote this".
OVERSIZED_SUMMARY = "超长摘要片段" * 20_000


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

    model = SummarizingRecording(says("nothing is left"), summary_text=OVERSIZED_SUMMARY)
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
    assert stored["summary"] == OVERSIZED_SUMMARY


async def test_a_summary_that_cuts_deeper_than_it_covers_falls_back(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    """A stored summary can pass the numeric coverage check
    (`stored.last_sequence >= this round's boundary`) and still be unusable,
    and the Run has to degrade to the structural plan rather than stretch to
    accommodate it.

    The name is historical and kept deliberately. `plan_context` used to
    *search* for a boundary: it walked forward until the round fit, so a text
    bigger than the terse structural sentence could buy itself room by cutting
    one message deeper than the range it was ever asked to explain — a
    `CompactionRecord` claiming message ids the summarizer never read. That
    move no longer exists: given a `CoveredSummary` the boundary is pinned to
    the range the text covers, and a text too big for that range simply does
    not fit, so nothing is compacted with it at all. This test's outcome is
    unchanged and still worth guarding — the fallback fires, the round goes
    out on the structural summary, and no model call is spent — but it now
    fires on `plan.fits`, not on a coverage comparison. See
    `test_summary_widening.py`, which asserts that distinction directly.
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
    # structural sentence that boundary was sized for.
    #
    # What the multiplier is calibrated to, since a later reader tuning it
    # needs the real reason: it is the size at which the text is too big to
    # stand at `baseline_last` while still being nowhere near
    # `test_a_summary_too_large_to_fit_falls_back_to_the_structural_plan`'s
    # 20_000-fold text, which no boundary in this history could absorb. The
    # gap between those two is what keeps this a distinct case rather than a
    # second copy of that test.
    #
    # It was originally calibrated for a different property — under the old
    # boundary *search* this was the exact size that fit at through 11 but not
    # at through 10, making the search step one message past its coverage.
    # `plan_context` pins the boundary now and never takes that step, so that
    # property is gone; the number is kept because the size relation above
    # still holds and still exercises the path this test is named for.
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
    # Fell back to the structural plan rather than stretching to fit the
    # planted text — and the structural plan compacts the same range the
    # planted text claimed, so nothing about the round's own boundary moved.
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


async def test_a_reused_summary_brings_a_run_back_under_the_cost_ceiling(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    small_endpoint: str,
    agent_on_the_small_endpoint: Any,
) -> None:
    """The mirror of the CRITICAL fix, one call earlier in the method.

    The cost gate has to front `_generate_summary` — the part that actually
    spends — and nothing before it. The reuse branch (step 2) makes no model
    call, so gating *that* on `baseline`'s estimate would measure a Run
    against the bigger structural plan when a shorter reused summary could
    have brought it back under the ceiling on its own: a Run paused that
    should have continued, the same shape as the Critical this file already
    covers, one step earlier in the method.

    Calibrated against `plan_context` and `projected_cost` directly (not
    guessed — the same approach
    `test_a_summary_that_cuts_deeper_than_it_covers_falls_back` uses, see
    that test's note). At this seed shape (`pairs=8, size=3_000`, the same
    conversation `test_a_summary_that_cuts_deeper_than_it_covers_falls_back`
    calibrates against) and $3/$15-per-million pricing, the structural
    baseline projects to $0.088866 and reusing the short summary planted
    below projects to $0.088599 — a ceiling of $0.0887 sits strictly between
    the two, refusing the first and allowing the second.
    """
    priced = client.post(
        f"/api/v1/model-endpoints/{small_endpoint}/pricing",
        headers=scope,
        json={"currency": "USD", "input_per_million": "3", "output_per_million": "15"},
    )
    assert priced.status_code == 201, priced.text
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id, pairs=8, size=3_000)
    await _plant_summary_row(
        engine,
        session_id,
        workspace_id,
        first_sequence=1,
        last_sequence=10,
        text_body="已处理，无新增。",
    )
    await _set_ceiling(engine, workspace_id, "0.0887")

    model = SummarizingRecording(says("nothing is left"))
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    reloaded = status(client, scope, run)
    assert reloaded["status"] == "completed"
    # And it got there on the reused summary, not by skipping compaction —
    # no model call was needed (the stored summary was sufficient), and the
    # round's own compaction record says so.
    assert model.calls == 0
    compacted = await payloads(engine, run, "context_compacted")
    assert len(compacted) == 1
    assert compacted[0]["source"] == "model"


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


# -- Task 4: a declared summary endpoint actually changes where the call goes


async def test_the_summary_call_is_routed_to_the_declared_summary_endpoint(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
    small_endpoint: str,
    summary_endpoint: str,
) -> None:
    """`test_a_saved_summary_records_which_model_wrote_it` above only proves
    what got written to `session_compactions` afterward — an implementation
    that resolved the summary endpoint correctly for that one row but never
    actually dispatched the call to it would still pass that test. This
    proves the call itself: what `request.policy.endpoint_id` was on the
    exact `ModelRequest` the spy received, at the same boundary a real
    `ModelRouter` would read it from (`_endpoint_of`).

    Both halves matter. Only checking the summary call would miss a bug that
    routes the *ordinary* round to the summarizer too — worse than not
    routing at all, since it would answer the user's own turn from the wrong
    model.
    """
    agent = agent_on_the_small_endpoint(summary_endpoint_id=summary_endpoint)
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    model = SummarizingRecording(says("nothing is left"))
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    assert model.calls == 1
    assert _endpoint_of(model.summary_requests[0]) == UUID(summary_endpoint)

    ordinary = [request for request in model.requests if not _is_summary_request(request)]
    assert ordinary
    assert all(_endpoint_of(request) == UUID(small_endpoint) for request in ordinary)


async def test_no_summary_endpoint_means_the_summary_call_uses_the_agent_s_own(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
    small_endpoint: str,
) -> None:
    """The default half of the same claim: an Agent that names no separate
    summary endpoint gets its summarization call dispatched to its own —
    not merely a policy object that says so (`_summary_policy`'s own return
    value), but the request as the spy actually received it.
    """
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    model = SummarizingRecording(says("nothing is left"))
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    assert model.calls == 1
    assert _endpoint_of(model.summary_requests[0]) == UUID(small_endpoint)


# -- Task 5: a summarization call spends real money and must be accounted for


def _price(
    client: TestClient,
    scope: dict[str, str],
    endpoint_id: str,
    *,
    input_price: str,
    output_price: str,
) -> None:
    priced = client.post(
        f"/api/v1/model-endpoints/{endpoint_id}/pricing",
        headers=scope,
        json={
            "currency": "USD",
            "input_per_million": input_price,
            "output_per_million": output_price,
        },
    )
    assert priced.status_code == 201, priced.text


async def test_a_successful_summary_call_is_billed_to_the_runs_shared_budget(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
    small_endpoint: str,
) -> None:
    """§12.4's gap this closes: a summarization call is a real call on a real
    endpoint, and its usage and cost have to land on the same
    `run_budget_scopes` row every other model call accrues to — not nowhere.

    Both the ordinary round and the summary call are given explicit token
    counts here (`cost_of` would otherwise call either one "unknown", and one
    unknown round poisons the Run's whole total to unknown per §12.4 — see
    `pricing.py`'s `_accumulate_cost`), so the persisted total can be checked
    against a real number instead of merely "not zero".
    """
    _price(client, scope, small_endpoint, input_price="3", output_price="15")
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    ordinary_answer = ModelResponse(
        stop_reason=StopReason.COMPLETED,
        text="nothing is left",
        input_tokens=100,
        output_tokens=20,
    )
    model = SummarizingRecording(
        ordinary_answer, summary_input_tokens=500, summary_output_tokens=50
    )
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    assert model.calls == 1

    prices = TokenPrices(
        currency="USD", input_per_million=Decimal("3"), output_per_million=Decimal("15")
    )
    expected = cost_of(prices, input_tokens=100, output_tokens=20).plus(
        cost_of(prices, input_tokens=500, output_tokens=50)
    )
    row = await _budget_row(engine, UUID(run))
    assert row is not None
    assert Decimal(row["consumed_cost"]) == expected.amount
    assert row["consumed_tokens"] == (100 + 20) + (500 + 50)
    assert row["cost_quality"] == "provider"


async def test_a_refused_summary_that_reported_usage_is_still_billed(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
    small_endpoint: str,
) -> None:
    """Decision 2: accrue whenever the provider reported usage, whatever the
    `stop_reason` — a call that produced tokens and then failed was still
    paid for on the real endpoint. `RefusingSummarizer`'s answer here is
    exactly `test_a_refused_summary_is_logged_same_as_a_timeout`'s (a
    non-`completed` stop reason, degrading to the structural summary), the
    only difference is that this one carries usage — proving the billing
    path does not depend on the call having succeeded.
    """
    _price(client, scope, small_endpoint, input_price="3", output_price="15")
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    model = RefusingSummarizer(
        says("nothing is left"), summary_input_tokens=500, summary_output_tokens=50
    )
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    compacted = await payloads(engine, run, "context_compacted")
    assert compacted[0]["source"] == "structural"

    billed = await payloads(engine, run, "context_summary_billed")
    assert len(billed) == 1
    assert billed[0]["input_tokens"] == 500
    assert billed[0]["output_tokens"] == 50

    prices = TokenPrices(
        currency="USD", input_per_million=Decimal("3"), output_per_million=Decimal("15")
    )
    expected = cost_of(prices, input_tokens=500, output_tokens=50)
    assert Decimal(billed[0]["cost"]) == expected.amount

    row = await _budget_row(engine, UUID(run))
    assert row is not None
    assert row["consumed_tokens"] >= 550


async def test_a_summary_call_that_never_reached_the_provider_bills_nothing(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
    small_endpoint: str,
) -> None:
    """The other half of decision 2: only a call that never reached the
    provider costs nothing. `FailingSummarizer` raises before any
    `ModelResponse` exists at all, so there is no usage to have reported —
    unlike `RefusingSummarizer` above, which answers with a `failed`
    `stop_reason` but still carries token counts.
    """
    _price(client, scope, small_endpoint, input_price="3", output_price="15")
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    model = FailingSummarizer(says("nothing is left"))
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    assert await payloads(engine, run, "context_summary_billed") == []

    row = await _budget_row(engine, UUID(run))
    assert row is not None
    # Not `consumed_cost`: the ordinary round's own answer (`says(...)`) also
    # reports no usage, which already makes the Run's total "unknown" on its
    # own (§12.4) — a fact this test does not care about and must not let
    # mask the one it does. `consumed_tokens` only ever moves on a real
    # billable count, so it is the one field a no-op summary call cannot
    # touch by accident.
    assert row["consumed_tokens"] == 0


async def test_the_summary_call_is_billed_at_the_summary_endpoints_own_price(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
    small_endpoint: str,
    summary_endpoint: str,
) -> None:
    """Decision 1: price it with the summary endpoint's own prices, not the
    main endpoint's. Priced wildly apart (the main endpoint here would bill
    500x what the summary endpoint would for the same tokens) so a bug that
    billed the summary call at the main endpoint's rate could not pass by
    coincidence.
    """
    _price(client, scope, small_endpoint, input_price="1500", output_price="1500")
    _price(client, scope, summary_endpoint, input_price="3", output_price="15")
    agent = agent_on_the_small_endpoint(summary_endpoint_id=summary_endpoint)
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    ordinary_answer = ModelResponse(
        stop_reason=StopReason.COMPLETED, text="nothing is left", input_tokens=1, output_tokens=1
    )
    model = SummarizingRecording(
        ordinary_answer, summary_input_tokens=500, summary_output_tokens=50
    )
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    billed = await payloads(engine, run, "context_summary_billed")
    assert len(billed) == 1
    assert billed[0]["endpoint_id"] == summary_endpoint

    summary_prices = TokenPrices(
        currency="USD", input_per_million=Decimal("3"), output_per_million=Decimal("15")
    )
    expected = cost_of(summary_prices, input_tokens=500, output_tokens=50)
    assert Decimal(billed[0]["cost"]) == expected.amount
    assert billed[0]["cost_currency"] == "USD"


async def test_a_summary_calls_cost_can_push_a_later_round_past_the_ceiling(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
    small_endpoint: str,
) -> None:
    """Decision 4, end to end: once accrued, the summary's cost has to be
    what `_cost_precheck` sees on the *next* round — the whole reason this
    task exists ("it can never itself push a Run to its §12.4 ceiling").

    One Run, two rounds, driven by one `drive()` call: round one triggers
    compaction and pays for a (deliberately huge) summary; round one's own
    tool-call answer and round two's precheck are both tiny and both known,
    so nothing but the summary's own cost can be what pushes the ceiling.
    Two separate Runs would not prove this — each `ask()` opens its own
    fresh `run_budget_scopes` row (`accept_run`'s `budget_root_run_id=run_id`),
    so only rounds *inside* one Run's slice share a budget to push.
    """
    _price(client, scope, small_endpoint, input_price="3", output_price="15")
    agent = agent_on_the_small_endpoint(["shell.exec"])
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)
    await _set_ceiling(engine, workspace_id, "5")

    tool_round = ModelResponse(
        stop_reason=StopReason.TOOL_CALL,
        text="",
        tool_calls=(
            ToolCallBlock(call_id="c1", name="shell.exec", arguments={"command": "./suite"}),
        ),
        input_tokens=1,
        output_tokens=1,
    )
    final_round = ModelResponse(
        stop_reason=StopReason.COMPLETED, text="done", input_tokens=1, output_tokens=1
    )
    model = SummarizingRecording(
        tool_round,
        final_round,
        summary_input_tokens=2_000_000,
        summary_output_tokens=200_000,
    )
    run = ask(client, scope, session, "run the suite")
    await drive(engine, model, StandInSandbox())

    reloaded = status(client, scope, run)
    # The summarizer was asked exactly once — round two never regenerates a
    # summary it can reuse (or, failing that, its own §12.4 gate in
    # `_plan_context` refuses a second one before it is ever asked, the same
    # gate `test_the_summarizer_is_never_asked_once_the_cost_ceiling_is_reached`
    # covers) — so nothing here can be explained by a second huge charge.
    assert model.calls == 1
    assert reloaded["status"] == "paused"
    assert reloaded["pause_reason"] == "limit"


# -- Review fixes: price pinning, the call-count valve, and an unpriced
# declared summary endpoint's consequence


async def test_the_default_summary_call_is_billed_at_the_runs_pinned_main_price(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
    small_endpoint: str,
) -> None:
    """§12.4 pins a Run's price at creation so a mid-Run repricing cannot
    change what it is charged. The default summary call (no
    `summary_endpoint_id` declared) resolves to this Run's own main
    endpoint — `_summary_policy` returns the main policy unchanged — so its
    price is that same pin, `context.prices`, not a fresh read. Reading
    live here would let one endpoint answer at two different prices within
    one Run depending only on which call asked.
    """
    _price(client, scope, small_endpoint, input_price="3", output_price="15")
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    run = ask(client, scope, session, "and what is left?")
    # Repriced after the Run was created — and so after its
    # `model_pricing_version_id` was already pinned — but before the Worker
    # ever runs a round. The ordinary shape of "an admin changed the price
    # mid-Run".
    _price(client, scope, small_endpoint, input_price="30", output_price="150")

    ordinary_answer = ModelResponse(
        stop_reason=StopReason.COMPLETED, text="nothing is left", input_tokens=1, output_tokens=1
    )
    model = SummarizingRecording(
        ordinary_answer, summary_input_tokens=500, summary_output_tokens=50
    )
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    billed = await payloads(engine, run, "context_summary_billed")
    assert len(billed) == 1

    pinned_price = TokenPrices(
        currency="USD", input_per_million=Decimal("3"), output_per_million=Decimal("15")
    )
    expected = cost_of(pinned_price, input_tokens=500, output_tokens=50)
    assert Decimal(billed[0]["cost"]) == expected.amount

    # Not the live $30/$150 price — billing that instead is exactly the bug
    # this test guards against.
    live_price = TokenPrices(
        currency="USD", input_per_million=Decimal("30"), output_per_million=Decimal("150")
    )
    wrong = cost_of(live_price, input_tokens=500, output_tokens=50)
    assert Decimal(billed[0]["cost"]) != wrong.amount


async def test_a_summary_call_counts_against_the_max_model_calls_ceiling(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
) -> None:
    """Product decision, §12.4: the call counter moves for a summarization
    call too, not only tokens and cost — it is the one valve that still
    works with no price and no cost ceiling configured at all, which is the
    default shape a deployment starts in. No pricing and no `_set_ceiling`
    call anywhere in this test, deliberately: what stops this Run has to be
    the call counter alone.
    """
    agent = agent_on_the_small_endpoint(max_model_calls=1)
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    model = SummarizingRecording(
        says("nothing is left"), summary_input_tokens=10, summary_output_tokens=10
    )
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    reloaded = status(client, scope, run)
    # One round hides two calls: the summarizer, then the round's own
    # answer. `max_model_calls=1` allows only one, so the Run stops here —
    # even though its own answer looked complete, and even though no cost
    # ceiling was ever configured to stop it.
    assert model.calls == 1
    assert reloaded["status"] == "paused"
    assert reloaded["pause_reason"] == "limit"
    assert reloaded["budget"]["consumed_model_calls"] == 2


async def test_an_unpriced_declared_summary_endpoint_makes_the_runs_cost_unknown(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
    small_endpoint: str,
    summary_endpoint: str,
) -> None:
    """Decision, §12.4: an unpriced declared summary endpoint is accepted at
    publish — the same choice this platform already makes for an unpriced
    *main* endpoint — and bills `unknown()`, which then poisons the whole
    Run-tree's `consumed_cost` forever. Not a special case: the ordinary
    §12.4 rule ("one unpriced round makes the whole total unknown") working
    on a new source of a round.
    """
    _price(client, scope, small_endpoint, input_price="3", output_price="15")
    # summary_endpoint is deliberately left unpriced.
    agent = agent_on_the_small_endpoint(summary_endpoint_id=summary_endpoint)
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    ordinary_answer = ModelResponse(
        stop_reason=StopReason.COMPLETED, text="nothing is left", input_tokens=1, output_tokens=1
    )
    model = SummarizingRecording(
        ordinary_answer, summary_input_tokens=500, summary_output_tokens=50
    )
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    row = await _budget_row(engine, UUID(run))
    assert row is not None
    assert row["cost_quality"] == "unknown"
    assert row["consumed_cost"] is None

