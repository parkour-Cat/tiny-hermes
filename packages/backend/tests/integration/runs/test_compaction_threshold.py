"""一个 Agent author 写下的 compaction_threshold，真的改了一轮的结果——不只是
被读到了。

`test_budget_hash.py`（unit/agents）证明了这个字段发布时会被拒绝或接受、哈希
会不会变；`test_compaction_threshold.py`（unit/runs）证明了 `plan_context`
纯函数本身对 `threshold=` 的反应。两者都没有证明一件事：一个作者在草稿里写
的这个数字，穿过发布、穿过 Worker 的 `_compaction_threshold`，最终真的改变了
一轮实际发生了什么。这正是 CLAUDE.md 点名的那种 bug——写进去了，没人够得着——
所以这里不读 `plan_context` 的返回值，读的是 `CONTEXT_COMPACTED` 事件：
`plan_context` 被传了一个数字只证明有人调用过它，事件被写出来才证明那个数字
真的改变了这一轮。
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..conftest import VALID_SPEC
from .test_context_budget import SMALL_ENDPOINT, ask, fails, payloads, says, start_session, status
from .test_worker_tools import Recording, drive

CREDENTIAL = "TINY_HERMES_TEST_COMPACTION_THRESHOLD_KEY"

#: `SMALL_ENDPOINT`'s allowance is 9,472 (`test_context_budget.py::ALLOWANCE`).
#: Eight seeded turns of 1,000 characters each land the round at roughly a
#: third of it — comfortably past `MIN_COMPACTION_THRESHOLD` (0.20),
#: comfortably short of `DEFAULT_COMPACTION_THRESHOLD` (0.50), so the same
#: seeded conversation lands on opposite sides of the two thresholds under
#: test without needing two different fixtures.
TURN_SIZE = 1_000
TURN_PAIRS = 4


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


def _publish(
    client: TestClient,
    scope: dict[str, str],
    endpoint_id: str,
    *,
    compaction_threshold: float | None,
) -> str:
    alias = f"threshold-{uuid4().hex[:8]}"
    agent = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Threshold", "alias": alias}
    ).json()
    spec: dict[str, Any] = {
        **VALID_SPEC,
        "tools": [],
        "model_policy": {"provider": "openai_compatible", "endpoint_id": endpoint_id},
    }
    if compaction_threshold is not None:
        spec["context_budget"] = {"compaction_threshold": compaction_threshold}
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


async def _seed_old_turns(
    engine: AsyncEngine, session_id: UUID, workspace_id: UUID, *, pairs: int, size: int
) -> None:
    """Old conversation, written straight into `session_messages`.

    Duplicated from `test_compaction_event.py`'s helper of the same name
    rather than imported — this directory's convention (see that file's own
    note) is to not reach across files for underscore-prefixed helpers.
    Bypasses a real Run: the compaction boundary only ever reads this table
    (`ExecutionContext.history`), so nothing here needs a scripted tool round.
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
                        "c": '{"parts": [{"type": "text", "text": "'
                        + (filler * size)
                        + '"}]}',
                    },
                )
        await connection.execute(
            text(
                "UPDATE sessions SET next_message_sequence = :next "
                "WHERE id = :s AND next_message_sequence <= :seq"
            ),
            {"s": session_id, "seq": sequence, "next": sequence + 1},
        )


async def test_a_declared_threshold_moves_when_compaction_starts(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    small_endpoint: str,
) -> None:
    """The same seeded conversation, two Agents, one difference between
    them: `compaction_threshold`. The default (0.50) never sees this round
    cross its ratio and sends it untouched; `0.20` does, and only a `Worker`
    that actually read the author's number off the published spec — through
    `_compaction_threshold`, not a hardcoded default — could tell them apart.
    """
    workspace_id = UUID(scope["X-Workspace-Id"])

    default_agent = _publish(client, scope, small_endpoint, compaction_threshold=None)
    default_session = start_session(client, scope, default_agent)
    await _seed_old_turns(
        engine, UUID(default_session), workspace_id, pairs=TURN_PAIRS, size=TURN_SIZE
    )
    default_run = ask(client, scope, default_session, "and what is left?")
    default_model = Recording(says("nothing is left"))
    await drive(engine, default_model, None)

    assert status(client, scope, default_run)["status"] == "completed"
    assert await payloads(engine, default_run, "context_compacted") == []

    aggressive_agent = _publish(client, scope, small_endpoint, compaction_threshold=0.20)
    aggressive_session = start_session(client, scope, aggressive_agent)
    await _seed_old_turns(
        engine, UUID(aggressive_session), workspace_id, pairs=TURN_PAIRS, size=TURN_SIZE
    )
    aggressive_run = ask(client, scope, aggressive_session, "and what is left?")
    # Compacting means the Worker also attempts the auxiliary summarization
    # call `_plan_context` makes before it answers the round's own question
    # (see `test_context_budget.py::test_an_old_conversation_is_compacted_
    # with_its_range_and_ids`) — scripted to fail so this test pins the
    # structural shape §7.4.2 requires either way, not what a summarizer
    # happens to say.
    aggressive_model = Recording(fails(), says("nothing is left"))
    await drive(engine, aggressive_model, None)

    assert status(client, scope, aggressive_run)["status"] == "completed"
    compacted = await payloads(engine, aggressive_run, "context_compacted")
    assert len(compacted) == 1
    assert compacted[0]["source"] == "structural"


async def test_a_requested_compaction_happens_even_far_below_the_threshold(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    small_endpoint: str,
) -> None:
    """`/compact` 让人自己决定什么时候压，不必等阈值。

    判据和上面那条一样是 `CONTEXT_COMPACTED` 事件，不是标记有没有被写进去
    ——标记写进去了而没人读，正是这个仓库最常见的那种 bug，而这条链路一共有
    四层（命令、入站、store、Worker），任何一层断了都会让「写进去了」为真而
    「压缩发生了」为假。

    用默认阈值（0.50）的那个 Agent：上一条测试已经证明同样这段历史在它手里
    **不会**触发压缩。所以这里如果压了，只可能是因为那个请求被读到了。
    """
    workspace_id = UUID(scope["X-Workspace-Id"])

    agent = _publish(client, scope, small_endpoint, compaction_threshold=None)
    session = start_session(client, scope, agent)
    await _seed_old_turns(
        engine, UUID(session), workspace_id, pairs=TURN_PAIRS, size=TURN_SIZE
    )

    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE sessions SET compaction_requested_at = now() WHERE id = :s"),
            {"s": UUID(session)},
        )

    run = ask(client, scope, session, "and what is left?")
    model = Recording(fails(), says("nothing is left"))
    await drive(engine, model, None)

    assert status(client, scope, run)["status"] == "completed"
    compacted = await payloads(engine, run, "context_compacted")
    assert len(compacted) == 1

    # 标记被消费掉了：一次请求只压一次，不会在后面每一轮都重来。
    async with engine.connect() as connection:
        left = (
            await connection.execute(
                text("SELECT compaction_requested_at FROM sessions WHERE id = :s"),
                {"s": UUID(session)},
            )
        ).scalar_one()
    assert left is None


async def test_a_compaction_run_compacts_and_stops_without_answering(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    small_endpoint: str,
) -> None:
    """`purpose='compaction'` 的 Run 压完就结束，**不回答任何问题**。

    用户发的是 `/compact`，没有问题要答——多调一次模型是白花钱，而这个 Run
    存在的全部理由恰恰就是给那次摘要调用一个付账的地方。

    判据是两条一起：压了（`CONTEXT_COMPACTED` 事件在），**并且**模型脚本里那条
    「回答」一次都没被取走。只断言第一条的话，一个压完又顺手答了一句的实现也会
    通过——而那正是这条测试要排除的东西。
    """
    workspace_id = UUID(scope["X-Workspace-Id"])

    agent = _publish(client, scope, small_endpoint, compaction_threshold=None)
    session = start_session(client, scope, agent)
    await _seed_old_turns(
        engine, UUID(session), workspace_id, pairs=TURN_PAIRS, size=TURN_SIZE
    )

    # 经普通入口建 Run、再把 purpose 改掉，而不是直接 INSERT 一行：Run 的必填
    # 列太多，手拼一行等于把「一个 Run 长什么样」这件事在测试里再实现一遍，而那
    # 正是这个仓库反复吃亏的那种夹具。
    run_id = ask(client, scope, session, "/compact")
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET purpose = 'compaction' WHERE id = :r"),
            {"r": UUID(run_id)},
        )
        # `purpose` 只说「这个 Run 不回答」，不说「一定要压」。真正让压缩发生的
        # 是那个标记——`/compact` 两样都会设，这里也两样都设，否则这条测试测到的
        # 是「一个什么都不做的 Run」。
        await connection.execute(
            text("UPDATE sessions SET compaction_requested_at = now() WHERE id = :s"),
            {"s": UUID(session)},
        )

    # 脚本里两条：第一条给摘要调用，第二条是「回答」。第二条必须一个字都没被用掉。
    model = Recording(fails(), says("这句话不该被发出去"))
    await drive(engine, model, None)

    assert status(client, scope, run_id)["status"] == "completed"
    compacted = await payloads(engine, run_id, "context_compacted")
    assert len(compacted) == 1
    # 只发生过一次模型调用——就是那次摘要。压缩 Run 不该再调模型回答问题。
    assert len(model.requests) == 1, [r.messages for r in model.requests]
