"""The reply, from a finished Run back to the person who typed the message.

Written to go in through the webhook rather than by seeding rows, because
the thing most worth proving is the join between the two halves. This
platform has shipped a working inbound path whose result nobody could
reach — twice in this module alone — and a dispatcher tested against
hand-seeded `channel_events` would prove the query and not the path.

So every test here starts with a real HTTP delivery, lets it become a real
Run, finishes that Run the way the Worker would, and then asks whether
anything comes out the other side.
"""

import base64
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.channels.application.outbound import (
    ChannelReplyDispatcher,
    ReplyOutcome,
)
from tiny_hermes.channels.infrastructure.feishu_sender import FeishuApiRefused
from tiny_hermes.channels.infrastructure.sql_channel_store import SqlChannelStore
from tiny_hermes.model_catalog.infrastructure.credentials import CredentialResolver
from tiny_hermes.secrets.infrastructure.sql_store import SqlSecretStore

KEY = "tenant-encrypt-key"
SECRET_ENV = "FEISHU_APP_SECRET_FOR_TEST"


def _encrypt(envelope: dict[str, Any]) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    plaintext = json.dumps(envelope).encode()
    pad = 16 - (len(plaintext) % 16)
    iv = b"0123456789abcdef"
    encryptor = Cipher(
        algorithms.AES(hashlib.sha256(KEY.encode()).digest()), modes.CBC(iv)
    ).encryptor()
    blob = iv + encryptor.update(plaintext + bytes([pad]) * pad) + encryptor.finalize()
    return json.dumps({"encrypt": base64.b64encode(blob).decode()}).encode()


def _headers(body: bytes) -> dict[str, str]:
    signature = hashlib.sha256(b"1755830400" + b"n1" + KEY.encode() + body).hexdigest()
    return {
        "X-Lark-Request-Timestamp": "1755830400",
        "X-Lark-Request-Nonce": "n1",
        "X-Lark-Signature": signature,
        "Content-Type": "application/json",
    }


def _message(event_id: str = "om_1") -> dict[str, Any]:
    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_zhang"}},
            "message": {"content": json.dumps({"text": "上周几单？"})},
        },
    }


class _Sender:
    """A channel sender that records, and optionally refuses."""

    def __init__(self, refusing: Exception | None = None) -> None:
        self.refusing = refusing
        self.sent: list[dict[str, str]] = []

    async def send_text(
        self, *, app_id: str, app_secret: str, open_id: str, text: str
    ) -> None:
        self.sent.append(
            {
                "app_id": app_id,
                "app_secret": app_secret,
                "open_id": open_id,
                "text": text,
            }
        )
        if self.refusing is not None:
            raise self.refusing


async def _binding(
    engine: AsyncEngine,
    workspace_id: str,
    agent_id: str,
    *,
    app_secret_ref: str | None = SECRET_ENV,
    status: str = "active",
) -> UUID:
    binding_id = uuid4()
    async with engine.begin() as connection:
        owner = await connection.execute(text("SELECT id FROM users LIMIT 1"))
        await connection.execute(
            text(
                "INSERT INTO channel_bindings"
                " (id, workspace_id, channel, agent_id, status, created_by,"
                "  created_at, encrypt_key_ref, app_id, app_secret_ref)"
                " VALUES (:i, :w, 'feishu', :a, :s, :u, now(),"
                "         'FEISHU_TEST_KEY', 'cli_x', :sec)"
            ),
            {
                "i": binding_id,
                "w": UUID(workspace_id),
                "a": UUID(agent_id),
                "s": status,
                "u": owner.scalar_one(),
                "sec": app_secret_ref,
            },
        )
    return binding_id


def _deliver(client: TestClient, binding_id: UUID, event_id: str = "om_1") -> UUID:
    body = _encrypt(_message(event_id))
    posted = client.post(
        f"/api/v1/channels/feishu/{binding_id}/webhook",
        content=body,
        headers=_headers(body),
    )
    assert posted.status_code == 200, posted.text
    run_id = posted.json()["run_id"]
    assert run_id is not None, posted.text
    return UUID(run_id)


async def _finish(
    engine: AsyncEngine, run_id: UUID, *, said: str, status: str = "completed"
) -> None:
    """What the Worker does at the end of a Run, in one statement.

    Only the two facts the dispatcher reads: the terminal state, and the
    assistant turn it will quote. Driving a real model here would test the
    Worker, which has its own suite.
    """
    async with engine.begin() as connection:
        row = await connection.execute(
            text("SELECT session_id, workspace_id FROM runs WHERE id = :r"),
            {"r": run_id},
        )
        session_id, workspace_id = row.one()
        await connection.execute(
            text("UPDATE runs SET status = :s, finished_at = now() WHERE id = :r"),
            {"r": run_id, "s": status},
        )
        if said:
            await connection.execute(
                text(
                    "INSERT INTO session_messages"
                    " (id, session_id, workspace_id, sequence, role, content,"
                    "  source_run_id, redacted, created_at)"
                    " VALUES (gen_random_uuid(), :s, :w,"
                    "  (SELECT coalesce(max(sequence), 0) + 1 FROM session_messages"
                    "   WHERE session_id = :s), 'assistant', :c, :r, false, now())"
                ),
                {
                    "s": session_id,
                    "w": workspace_id,
                    "c": json.dumps({"parts": [{"type": "text", "text": said}]}),
                    "r": run_id,
                },
            )


async def _dispatch(engine: AsyncEngine, sender: _Sender, **kwargs: Any) -> int:
    """One pass of the scan, in its own session.

    `CredentialResolver` with no KEK on purpose: these bindings name an
    environment variable, which is the form that needs no unwrapping. A
    Secret-id reference goes through the same resolver in production, and it
    is the resolver — not this dispatcher — that knows the difference.
    """
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        resolver = CredentialResolver(SqlSecretStore(db), None)
        dispatched = await ChannelReplyDispatcher(
            store=SqlChannelStore(db),
            resolve_secret=resolver.resolve,
            sender=sender,
            **kwargs,
        ).dispatch_once()
        await db.commit()
    return dispatched


async def _reply_row(engine: AsyncEngine) -> tuple[Any, int, str | None]:
    async with engine.connect() as connection:
        row = await connection.execute(
            text("SELECT replied_at, reply_attempts, reply_note FROM channel_events")
        )
        replied_at, attempts, note = row.one()
    return replied_at, attempts, note


async def test_a_finished_run_answers_the_person_who_sent_the_message(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole outbound half, over the same door the message came in.

    Asserting on the sender rather than on a log line: "the platform logged
    that it replied" is exactly the sentence this repository has believed
    five times while the person on the other end saw nothing.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)
    await _finish(engine, run_id, said="上周有 12 单。")

    sender = _Sender()
    assert await _dispatch(engine, sender) == 1

    assert len(sender.sent) == 1
    assert sender.sent[0]["open_id"] == "ou_zhang"
    assert sender.sent[0]["app_id"] == "cli_x"
    assert sender.sent[0]["app_secret"] == "s3cret"  # noqa: S105
    assert sender.sent[0]["text"] == "上周有 12 单。"

    replied_at, attempts, note = await _reply_row(engine)
    assert replied_at is not None
    assert attempts == 1
    assert note == ReplyOutcome.SENT


async def test_the_delivery_that_produced_the_run_records_which_run(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`channel_events.run_id` had a writer and no caller.

    Found by reading the database of a live deployment: two events, two
    Runs, both `run_id` columns NULL. The column is how a delivery is traced
    to its work, and the dispatcher's queue is keyed on it — so this is not
    a missing audit field any more, it is the reason a reply happens at all.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)

    run_id = _deliver(client, binding_id)

    async with engine.connect() as connection:
        recorded = await connection.execute(
            text("SELECT run_id FROM channel_events WHERE channel_binding_id = :b"),
            {"b": binding_id},
        )
    assert recorded.scalar_one() == run_id


async def test_a_reply_is_sent_once_however_often_the_dispatcher_runs(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan is a scan, so it runs again in a few seconds. Without the
    stamp the person gets the same answer every interval, forever."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)
    await _finish(engine, run_id, said="做完了")

    sender = _Sender()
    assert await _dispatch(engine, sender) == 1
    assert await _dispatch(engine, sender) == 0

    assert len(sender.sent) == 1


async def test_a_run_that_has_not_finished_is_left_alone(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-finished Run replied to is worse than a late reply: the
    delivery would be stamped and the real answer never sent."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    _deliver(client, binding_id)

    sender = _Sender()
    assert await _dispatch(engine, sender) == 0

    assert sender.sent == []
    replied_at, attempts, _ = await _reply_row(engine)
    assert replied_at is None
    assert attempts == 0


async def test_a_receive_only_binding_sends_nothing_and_stops_asking(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§929's drill binding. It must not send, and it must not sit in the
    queue being retried every interval for seven days either."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    binding_id = await _binding(engine, workspace_id, published_agent, app_secret_ref=None)
    run_id = _deliver(client, binding_id)
    await _finish(engine, run_id, said="做完了")

    sender = _Sender()
    await _dispatch(engine, sender)

    assert sender.sent == []
    replied_at, _, note = await _reply_row(engine)
    assert replied_at is not None
    assert note == ReplyOutcome.NO_CREDENTIAL


async def test_a_disabled_binding_is_not_replied_through(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An administrator shut this door while the Run was still working.

    The Run is finished and has an answer, and the answer still does not go
    out: a disabled binding is a decision about the channel, not about the
    Runs that happened to be in flight.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)
    await _finish(engine, run_id, said="做完了")
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE channel_bindings SET status = 'disabled' WHERE id = :b"),
            {"b": binding_id},
        )

    sender = _Sender()
    await _dispatch(engine, sender)

    assert sender.sent == []
    replied_at, _, note = await _reply_row(engine)
    assert replied_at is not None
    assert note == ReplyOutcome.BINDING_DISABLED


async def test_a_refused_send_is_retried_and_then_given_up_on(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feishu refusing is not the dispatcher failing.

    Retried, because a token expiring mid-flight or a momentary 5xx is
    ordinary. Given up on after a bound, because "bot is not in the chat"
    never becomes true by trying again, and a row that is retried forever is
    a scan that never drains.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)
    await _finish(engine, run_id, said="做完了")

    sender = _Sender(refusing=FeishuApiRefused(230001, "bot is not in the chat"))
    for _ in range(4):
        await _dispatch(engine, sender, max_attempts=3)

    assert len(sender.sent) == 3
    replied_at, attempts, note = await _reply_row(engine)
    assert attempts == 3
    assert replied_at is not None
    assert note is not None
    assert note.startswith(ReplyOutcome.REFUSED)


async def test_a_failed_run_reports_why_instead_of_going_quiet(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)
    await _finish(engine, run_id, said="", status="failed")
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET checkpoint = :c WHERE id = :r"),
            {"r": run_id, "c": json.dumps({"failure": "model_endpoint_unreachable"})},
        )

    sender = _Sender()
    await _dispatch(engine, sender)

    assert len(sender.sent) == 1
    assert "model_endpoint_unreachable" in sender.sent[0]["text"]


async def test_an_ordinary_console_run_is_never_in_the_queue(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    submitted_run: dict[str, Any],
) -> None:
    """Nothing pushes a console Run anywhere. The queue is `channel_events`,
    and a Run that arrived through the API put no row in it."""
    sender = _Sender()
    assert await _dispatch(engine, sender) == 0
    assert sender.sent == []
