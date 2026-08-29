"""运维看到一段被压缩过的会话，必须分得清模型读到的是什么。

一份语义摘要和一句「这里曾有 38 条消息」对后续每一轮的影响完全不同。事件里
不写，就只能靠猜。

`test_compaction_summary.py` 已经证明 `session_compactions` 那张表本身记对了
`source`/`endpoint_id`/`model`；这里证明的是另一件事——写进 `CONTEXT_COMPACTED`
事件 payload 的是不是同一份数据。这个项目最常见的 bug 是写进去了但没人够得着：
存摘要那步答对了，不代表事件也答对了，两者是两条分开的代码路径
（`_save_summary` 和 `_record_planning`）。
"""

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..conftest import VALID_SPEC
from .test_compaction_summary import FailingSummarizer, SummarizingRecording
from .test_context_budget import SMALL_ENDPOINT, ask, payloads, says, start_session, status
from .test_worker_tools import drive

#: See `test_compaction_summary.py`'s own note on this: fixtures are rebuilt
#: per file rather than imported (importing a fixture and also naming it as
#: a parameter shadows itself for ruff's F811), and there is no precedent in
#: this directory for importing an underscore-prefixed helper across files
#: either — only `ask`/`payloads`/`says`/`start_session`/`status` and the
#: public test-double classes cross file boundaries (`Recording`,
#: `StandInSandbox` from `test_worker_tools.py` is the existing precedent
#: for that half).
CREDENTIAL = "TINY_HERMES_TEST_COMPACTION_EVENT_KEY"


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
    def build() -> str:
        alias = f"compaction-event-{uuid4().hex[:8]}"
        agent = client.post(
            "/api/v1/agents", headers=scope, json={"name": "CompactionEvent", "alias": alias}
        ).json()
        spec: dict[str, Any] = {
            **VALID_SPEC,
            "tools": [],
            "model_policy": {"provider": "openai_compatible", "endpoint_id": small_endpoint},
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


async def _seed_old_turns(
    engine: AsyncEngine,
    session_id: UUID,
    workspace_id: UUID,
    *,
    pairs: int = 2,
    size: int = 15_000,
) -> None:
    """Old conversation, written straight into `session_messages`.

    The same approach as `test_compaction_summary.py`'s own helper of the
    same name (and `test_hints_are_searchable.py` before it) — duplicated
    rather than imported, per this directory's convention of not reaching
    across test files for underscore-prefixed helpers (see the module note
    above). Bypasses a real Run on purpose: the compaction boundary only
    ever reads this table (`ExecutionContext.history`), so a Run that
    produced this content would cost the suite a scripted tool round for
    nothing this file checks.
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


async def test_a_model_summary_says_which_model_wrote_it(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    agent_on_the_small_endpoint: Any,
    small_endpoint: str,
) -> None:
    agent = agent_on_the_small_endpoint()
    session = start_session(client, scope, agent)
    session_id = UUID(session)
    workspace_id = UUID(scope["X-Workspace-Id"])
    await _seed_old_turns(engine, session_id, workspace_id)

    model = SummarizingRecording(says("nothing is left"))
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    # Read back from `run_events`, not from the Worker's in-memory plan or
    # from `session_compactions` — the recurring bug in this project is a
    # value that gets written somewhere but that the event a reader would
    # actually look at cannot reach.
    compacted = await payloads(engine, run, "context_compacted")
    assert len(compacted) == 1
    assert compacted[0]["source"] == "model"
    assert compacted[0]["endpoint_id"] == small_endpoint
    assert compacted[0]["model"] == SMALL_ENDPOINT["model"]


async def test_a_fallback_says_so_and_names_no_model(
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

    model = FailingSummarizer(says("nothing is left"))
    run = ask(client, scope, session, "and what is left?")
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    compacted = await payloads(engine, run, "context_compacted")
    assert len(compacted) == 1
    assert compacted[0]["source"] == "structural"
    # Explicit `None`, not an absent key: a payload that simply omitted
    # `endpoint_id`/`model` on this path would read the same as "we forgot
    # to record it" to anyone downstream, which is exactly the ambiguity
    # this event exists to remove.
    assert compacted[0]["endpoint_id"] is None
    assert compacted[0]["model"] is None
