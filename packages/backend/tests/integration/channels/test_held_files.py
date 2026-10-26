# pyright: reportPrivateUsage=false
# The helpers are borrowed from `test_reply_dispatch.py`, the module that
# already goes in through the webhook; a second copy would drift.
"""A Feishu message that is only a file waits for the sender's next one.

Found in the real chat, not here: a file alone became a Run, and the model,
told nothing, did whatever it guessed — an unasked-for summary sent back as
a spreadsheet. A file sent with its question became two Runs, and which one
did the work depended on which message landed first.

Every test goes in through the webhook, like `test_reply_dispatch.py`, and
out through the real dispatcher, because the hold is two halves — written on
the way in, read on the next message and by the scan — and the bug this
project keeps producing is the half nobody reads.
"""

import json
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.api.cli import _channel_gather
from tiny_hermes.runs.application.scheduler import SchedulerRuntime, SchedulerSettings
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.shared.config import Settings

from .test_reply_dispatch import (
    KEY,
    SECRET_ENV,
    _age_delivery,
    _binding,
    _deliver,
    _deliver_raw,
    _dispatch,
    _finish,
    _gather,
    _run_of,
    _Sender,
)


def _file(event_id: str, file_key: str, name: str) -> dict[str, Any]:
    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_zhang"}},
            "message": {
                "message_id": event_id,
                "message_type": "file",
                "content": json.dumps({"file_key": file_key, "file_name": name}),
            },
        },
    }


def _text(event_id: str, said: str) -> dict[str, Any]:
    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_zhang"}},
            "message": {"content": json.dumps({"text": said})},
        },
    }


async def _setup(
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> UUID:
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    return await _binding(engine, workspace_id, published_agent)


async def _run_count(engine: AsyncEngine, workspace_id: str) -> int:
    async with engine.connect() as connection:
        found = await connection.execute(
            text("SELECT count(*) FROM runs WHERE workspace_id = :w"),
            {"w": UUID(workspace_id)},
        )
        return found.scalar_one()


async def _files_on(engine: AsyncEngine, run_id: UUID) -> list[dict[str, Any]]:
    """The file parts of the user message this Run was started with."""
    async with engine.connect() as connection:
        found = await connection.execute(
            text(
                "SELECT content FROM session_messages"
                " WHERE source_run_id = :r AND role = 'user'"
            ),
            {"r": run_id},
        )
        content = found.scalar_one()
    document = cast(dict[str, Any], content if isinstance(content, dict) else json.loads(content))
    parts = cast(list[dict[str, Any]], document["parts"])
    return [part for part in parts if part.get("type") == "file"]


def _asks(sender: _Sender) -> list[dict[str, str]]:
    return [entry for entry in sender.sent if "需要我拿" in entry["text"]]


async def test_a_file_alone_starts_no_run(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)

    posted = _deliver_raw(client, binding_id, _file("om_f1", "file_v3_1", "orders.xlsx"))
    await _gather(engine)

    assert posted.status_code == 200, posted.text
    assert await _run_count(engine, workspace_id) == 0


async def test_the_next_message_carries_the_held_file(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _file("om_f1", "file_v3_1", "orders.xlsx"))

    run_id = await _deliver(client, engine, binding_id, "om_q1")

    files = await _files_on(engine, run_id)
    assert [(part["reference"], part["name"]) for part in files] == [
        ("feishu:om_f1:file_v3_1", "orders.xlsx")
    ]


async def test_a_held_file_goes_with_one_message_only(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _file("om_f1", "file_v3_1", "orders.xlsx"))
    await _deliver(client, engine, binding_id, "om_q1")

    second = await _deliver(client, engine, binding_id, "om_q2")

    assert await _files_on(engine, second) == []


async def test_a_file_alone_is_asked_about_after_a_few_seconds(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _file("om_f1", "file_v3_1", "orders.xlsx"))
    await _age_delivery(engine, 10)

    sender = _Sender()
    await _dispatch(engine, sender)
    await _dispatch(engine, sender)

    asks = _asks(sender)
    assert len(asks) == 1
    assert asks[0]["open_id"] == "ou_zhang"
    assert "《orders.xlsx》" in asks[0]["text"]


async def test_a_file_just_received_is_not_asked_about_yet(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The few seconds are what let a file and its question, sent together,
    go without a question in between."""
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _file("om_f1", "file_v3_1", "orders.xlsx"))

    sender = _Sender()
    await _dispatch(engine, sender)

    assert _asks(sender) == []


async def test_a_file_sent_with_its_question_is_not_asked_about(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _file("om_f1", "file_v3_1", "orders.xlsx"))
    await _deliver(client, engine, binding_id, "om_q1")
    await _age_delivery(engine, 10)

    sender = _Sender()
    await _dispatch(engine, sender)
    await _dispatch(engine, sender)

    assert _asks(sender) == []


async def test_several_files_are_asked_about_once(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _file("om_f1", "file_v3_1", "orders.xlsx"))
    _deliver_raw(client, binding_id, _file("om_f2", "file_v3_2", "contract.pdf"))
    await _age_delivery(engine, 10)

    sender = _Sender()
    await _dispatch(engine, sender)
    await _dispatch(engine, sender)

    asks = _asks(sender)
    assert len(asks) == 1
    assert "《orders.xlsx》" in asks[0]["text"]
    assert "《contract.pdf》" in asks[0]["text"]


async def test_new_drops_a_held_file(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/new` is starting over, and a file waiting for the old conversation
    does not belong in the new one — nor does a question about it."""
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _file("om_f1", "file_v3_1", "orders.xlsx"))
    _deliver_raw(client, binding_id, _text("om_new", "/new"))

    run_id = await _deliver(client, engine, binding_id, "om_q1")
    await _age_delivery(engine, 10)
    sender = _Sender()
    await _dispatch(engine, sender)

    assert await _files_on(engine, run_id) == []
    assert _asks(sender) == []


async def test_a_file_held_for_one_person_is_not_given_to_another(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _file("om_f1", "file_v3_1", "orders.xlsx"))
    someone_else = _text("om_li", "你好")
    someone_else["event"]["sender"]["sender_id"]["open_id"] = "ou_li"

    _deliver_raw(client, binding_id, someone_else)
    await _gather(engine)

    run_id = await _run_of(engine, binding_id, "om_li")
    assert run_id is not None
    assert await _files_on(engine, run_id) == []


async def _said_on(engine: AsyncEngine, run_id: UUID) -> list[str]:
    """The text parts of the user message this Run was started with."""
    async with engine.connect() as connection:
        found = await connection.execute(
            text(
                "SELECT content FROM session_messages"
                " WHERE source_run_id = :r AND role = 'user'"
            ),
            {"r": run_id},
        )
        content = found.scalar_one()
    document = cast(dict[str, Any], content if isinstance(content, dict) else json.loads(content))
    parts = cast(list[dict[str, Any]], document["parts"])
    return [str(part["text"]) for part in parts if part.get("type") == "text"]


async def test_a_question_that_arrives_before_its_file_still_gets_it(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real chat, second time: the words landed 0.25 s before the file.

    Feishu does not order the two. Whichever comes first, one Run has both.
    """
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _text("om_q1", "这个文件讲了什么"))
    _deliver_raw(client, binding_id, _file("om_f1", "file_v3_1", "notes.txt"))

    await _gather(engine)

    assert await _run_count(engine, workspace_id) == 1
    run_id = await _run_of(engine, binding_id, "om_f1")
    assert run_id is not None
    assert [part["reference"] for part in await _files_on(engine, run_id)] == [
        "feishu:om_f1:file_v3_1"
    ]
    assert "这个文件讲了什么" in await _said_on(engine, run_id)


async def test_two_quick_lines_become_one_run_in_the_order_sent(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _text("om_a", "第一句"))
    _deliver_raw(client, binding_id, _text("om_b", "第二句"))

    await _gather(engine)

    assert await _run_count(engine, workspace_id) == 1
    run_id = await _run_of(engine, binding_id, "om_b")
    assert run_id is not None
    said = await _said_on(engine, run_id)
    assert said[0].index("第一句") < said[0].index("第二句")


async def test_a_message_still_inside_the_window_is_not_started(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window is what lets a file that is 0.25 s behind still make it."""
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _text("om_q1", "在吗"))

    assert await _gather(engine, window_seconds=60) == 0
    assert await _run_count(engine, workspace_id) == 0


async def test_a_gathered_message_is_not_started_twice(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _text("om_q1", "在吗"))

    await _gather(engine)
    await _gather(engine)

    assert await _run_count(engine, workspace_id) == 1


async def test_a_gathered_batch_is_answered_once(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One Run, so one card and one answer — not one per message gathered,
    and not a held-file question about a file the Run already has."""
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _text("om_q1", "这个文件讲了什么"))
    _deliver_raw(client, binding_id, _file("om_f1", "file_v3_1", "notes.txt"))
    await _gather(engine)
    run_id = await _run_of(engine, binding_id, "om_f1")
    assert run_id is not None
    await _finish(engine, run_id, said="讲的是订单汇总。")
    await _age_delivery(engine, 10)

    sender = _Sender()
    await _dispatch(engine, sender)
    await _dispatch(engine, sender)

    cards = [entry for entry in sender.sent if entry["delivery_key"].endswith(":c")]
    assert len(cards) == 1
    assert len(sender.after_opening()) == 1
    assert "讲的是订单汇总。" in sender.after_opening()[0]["text"]
    assert _asks(sender) == []


async def test_the_scheduler_turns_a_held_message_into_a_run(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    """The gather is only reachable if the Scheduler runs it. Built with the
    factory the scheduler process itself uses, so a scan left out of the
    pass, or a factory never handed to the runtime, fails here — every other
    test in this file calls the gatherer directly and would stay green."""
    binding_id = await _setup(engine, workspace_id, published_agent, monkeypatch)
    _deliver_raw(client, binding_id, _text("om_q1", "在吗"))
    runtime = SchedulerRuntime(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        notifier=NullWakeUpNotifier(),
        settings=SchedulerSettings(max_recovery_attempts=3, event_retention_hours=168),
        gather=_channel_gather(settings.model_copy(update={"channel_gather_seconds": 0})),
    )

    await runtime.run_once()

    assert await _run_count(engine, workspace_id) == 1
    assert await _run_of(engine, binding_id, "om_q1") is not None
