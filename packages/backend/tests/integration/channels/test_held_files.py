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
from sqlalchemy.ext.asyncio import AsyncEngine

from .test_reply_dispatch import (
    KEY,
    SECRET_ENV,
    _age_delivery,
    _binding,
    _deliver,
    _deliver_raw,
    _dispatch,
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


def _command(event_id: str, said: str) -> dict[str, Any]:
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

    assert posted.status_code == 200, posted.text
    assert posted.json()["run_id"] is None
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

    run_id = _deliver(client, binding_id, "om_q1")

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
    _deliver(client, binding_id, "om_q1")

    second = _deliver(client, binding_id, "om_q2")

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
    _deliver(client, binding_id, "om_q1")
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
    _deliver_raw(client, binding_id, _command("om_new", "/new"))

    run_id = _deliver(client, binding_id, "om_q1")
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
    someone_else = _command("om_li", "你好")
    someone_else["event"]["sender"]["sender_id"]["open_id"] = "ou_li"

    posted = _deliver_raw(client, binding_id, someone_else)

    assert await _files_on(engine, UUID(posted.json()["run_id"])) == []
