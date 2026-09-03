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
from typing import Any, cast
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
# The *name* of an environment variable, which is what a
# `credential_ref` may be. Never a secret — that distinction is the
# whole point of the `_ref` columns.
SECRET_ENV = "FEISHU_APP_SECRET_FOR_TEST"  # noqa: S105


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
    """A channel sender that records, and optionally refuses.

    Also records which workspace it was asked for: the dispatcher picks a
    sender per workspace so the request leaves under that workspace's own
    egress scope, and a factory that ignored the argument would look
    identical from every other assertion here.
    """

    def __init__(
        self, refusing: Exception | None = None, card_id: str | None = "om_card"
    ) -> None:
        self.refusing = refusing
        #: What Feishu answers `send_card` with. `None` reproduces an answer
        #: carrying no id — the card exists but can never be updated.
        self.card_id = card_id
        self.sent: list[dict[str, str]] = []
        self.asked_for: list[UUID] = []

    @staticmethod
    def _readable(card: dict[str, Any]) -> str:
        """What a person reads off a card, flattened.

        Recorded instead of the raw JSON so these tests assert on what was
        said rather than on the vendor's element structure — the same
        distinction `CanonicalMessage.text` draws. A test matching on card
        JSON breaks the first time a card gains a divider.

        Walked explicitly rather than recursively: a generic walk also
        collects `config` values and reorders what it finds, and both showed
        up as failures the moment it was tried.
        """
        said: list[str] = []
        header: Any = card.get("header")
        if isinstance(header, dict):
            title: Any = cast(dict[str, Any], header).get("title")
            if isinstance(title, dict):
                said.append(str(cast(dict[str, Any], title).get("content", "")))
        raw: Any = card.get("elements")
        for entry in cast(list[Any], raw) if isinstance(raw, list) else []:
            if not isinstance(entry, dict):
                continue
            element = cast(dict[str, Any], entry)
            body: Any = element.get("text")
            if isinstance(body, dict):
                said.append(str(cast(dict[str, Any], body).get("content", "")))
            buttons: Any = element.get("actions")
            for item in cast(list[Any], buttons) if isinstance(buttons, list) else []:
                if not isinstance(item, dict):
                    continue
                action = cast(dict[str, Any], item)
                said.append(str(action.get("url", "")))
                label: Any = action.get("text")
                if isinstance(label, dict):
                    said.append(str(cast(dict[str, Any], label).get("content", "")))
        return "\n".join(said)

    @property
    def opening(self) -> dict[str, str]:
        """The card this delivery opened with.

        Recipient and credentials are asserted here rather than on a later
        entry: a patch names no recipient — the message id already fixes who
        sees it — so the opening card is where "sent to the right person, as
        the right app" is actually decided.
        """
        opened = [
            entry
            for entry in self.sent
            if entry.get("delivery_key", "").endswith(":c")
        ]
        assert opened, "no opening card was sent"
        return opened[0]

    def after_opening(self) -> list[dict[str, str]]:
        """Everything except the card each delivery now opens with.

        Every delivery that has a Run gets an opening card within a second —
        that is the whole point of the card design. So a test asking "was an
        answer sent" means everything *after* that, and one asking "was
        anything sent" would now always be true and stop testing anything.

        Identified by the delivery key's `:c` suffix rather than by the
        card's wording: the suffix is the contract the dispatcher actually
        commits to, and matching on Chinese text would break the first time
        somebody reworded a card.
        """
        return [
            entry
            for entry in self.sent
            if not entry.get("delivery_key", "").endswith(":c")
        ]

    def __call__(self, workspace_id: UUID) -> "_Sender":
        self.asked_for.append(workspace_id)
        return self

    async def send_text(
        self,
        *,
        app_id: str,
        app_secret: str,
        open_id: str,
        text: str,
        delivery_key: str | None = None,
    ) -> None:
        self.sent.append(
            {
                "kind": "text",
                "app_id": app_id,
                "app_secret": app_secret,
                "open_id": open_id,
                "text": text,
                "delivery_key": delivery_key or "",
            }
        )
        if self.refusing is not None:
            raise self.refusing

    async def update_card(
        self,
        *,
        app_id: str,
        app_secret: str,
        message_id: str,
        card: dict[str, Any],
    ) -> None:
        self.sent.append(
            {
                "kind": "patch",
                "app_id": app_id,
                "app_secret": app_secret,
                "open_id": "",
                "message_id": message_id,
                "text": self._readable(card),
                "delivery_key": "",
            }
        )
        if self.refusing is not None:
            raise self.refusing

    async def send_card(
        self,
        *,
        app_id: str,
        app_secret: str,
        open_id: str,
        card: dict[str, Any],
        delivery_key: str | None = None,
    ) -> str | None:
        self.sent.append(
            {
                "kind": "card",
                "app_id": app_id,
                "app_secret": app_secret,
                "open_id": open_id,
                "text": self._readable(card),
                "delivery_key": delivery_key or "",
            }
        )
        if self.refusing is not None:
            raise self.refusing
        return self.card_id


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
            senders=sender,
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
    await _dispatch(engine, sender)
    assert len(sender.after_opening()) == 1

    assert len(sender.after_opening()) == 1
    assert sender.opening["open_id"] == "ou_zhang"
    assert sender.opening["app_id"] == "cli_x"
    assert sender.opening["app_secret"] == "s3cret"  # noqa: S105
    assert "上周有 12 单。" in sender.after_opening()[0]["text"]

    replied_at, attempts, note = await _reply_row(engine)
    assert replied_at is not None
    assert attempts == 1
    assert note == ReplyOutcome.SENT


async def _withdraw(engine: AsyncEngine, message_id: UUID) -> None:
    """What `SqlRunStore.mark_withdrawn` does, without building one.

    Every other helper in this module reaches the database directly rather
    than through the layer under test — `_finish` stands in for the Worker,
    this stands in for the `/undo` command handler. Going through a second
    `SqlRunStore`/session here would only add a transaction boundary this
    test does not need.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE session_messages SET withdrawn_at = now() WHERE id = :m"),
            {"m": message_id},
        )


async def _redact(engine: AsyncEngine, message_id: UUID) -> None:
    """Sets the column §344's erasure is *named* after.

    Deliberately raw SQL, and not because it is convenient: **nothing in this
    repository writes this column.** `redacted` has been read-only since
    `0002` — today's erasure (`sql_subject_store._erase_end_user`) deletes the
    rows instead. So there is no production writer to route through, and this
    helper stands in for the one a future erasure would need.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE session_messages SET redacted = true WHERE id = :m"),
            {"m": message_id},
        )


async def test_a_redacted_answer_is_not_sent_as_the_reply(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pending_replies` was the one read point that did not filter this.

    Four others do — `sql_store.py`'s two, `sql_search.py`'s, and
    `copy_checkpoint_messages` — and `redacted` means §344's erasure, whose
    whole meaning is *as if it never existed*. This scan is the only one of
    the five that puts text **outside the platform**, into somebody's chat
    client, where no later erasure can reach it.

    **This is not a leak today**, and the test does not claim to fix one:
    nothing sets the column, so nothing is currently marked erased. It is
    that the one place where erasure would matter most is the one place that
    did not check — so whoever gives this column its first writer inherits a
    hole rather than a guard.

    Two assistant turns, as in the withdrawal test above and for the same
    reason: this has to prove the dispatcher *falls back* to the turn still
    standing, not merely that the erased text is absent — which a dispatcher
    that sent nothing at all would also satisfy.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)
    await _finish(engine, run_id, said="the answer that stands")
    await _finish(engine, run_id, said="the answer about to be erased")

    async with engine.connect() as connection:
        erased_id = (
            await connection.execute(
                text(
                    "SELECT id FROM session_messages WHERE source_run_id = :r "
                    "ORDER BY sequence DESC LIMIT 1"
                ),
                {"r": run_id},
            )
        ).scalar_one()
    await _redact(engine, erased_id)

    sender = _Sender()
    await _dispatch(engine, sender)

    assert "the answer that stands" in sender.after_opening()[0]["text"]
    assert "about to be erased" not in sender.after_opening()[0]["text"]


async def test_a_withdrawn_answer_is_not_sent_as_the_reply(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The channel and the model's own context must tell the same story.

    Task 3's four other read points keep a withdrawn message out of
    everywhere a model or a reader looks. This is a fifth: if the dispatcher
    still sent it, the person would receive an answer that is not in the
    model's history — and the moment they asked a follow-up about it, the
    model would have no idea what they meant. Nothing here is about a leak:
    the reply goes to the same person who withdrew the message.

    Two assistant turns rather than one, so this proves the dispatcher falls
    back to the turn still standing — not merely that the withdrawn text is
    absent, which a dispatcher that sent nothing at all would also satisfy.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)
    await _finish(engine, run_id, said="the earlier answer")
    await _finish(engine, run_id, said="the later answer, about to be withdrawn")

    async with engine.connect() as connection:
        withdrawn_id = (
            await connection.execute(
                text(
                    "SELECT id FROM session_messages WHERE source_run_id = :r "
                    "ORDER BY sequence DESC LIMIT 1"
                ),
                {"r": run_id},
            )
        ).scalar_one()
    await _withdraw(engine, withdrawn_id)

    sender = _Sender()
    await _dispatch(engine, sender)

    assert "the earlier answer" in sender.after_opening()[0]["text"]
    assert "the later answer" not in sender.after_opening()[0]["text"]


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
    await _dispatch(engine, sender)
    await _dispatch(engine, sender)

    assert len(sender.after_opening()) == 1


async def test_a_run_that_has_not_finished_is_left_alone(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-finished Run answered is worse than a late answer: the
    delivery would be stamped and the real answer never sent.

    The opening card is excluded — it says 「正在处理」, which is exactly
    what an unfinished Run should be showing.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    _deliver(client, binding_id)

    sender = _Sender()
    await _dispatch(engine, sender)
    assert sender.after_opening() == []

    assert sender.after_opening() == []
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

    assert sender.after_opening() == []
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

    assert sender.after_opening() == []
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

    assert len(sender.after_opening()) == 3
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

    assert len(sender.after_opening()) == 1
    assert "model_endpoint_unreachable" in sender.after_opening()[0]["text"]


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
    await _dispatch(engine, sender)
    assert sender.after_opening() == []
    assert sender.after_opening() == []


async def test_the_reply_leaves_under_its_own_workspace_scope(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§16.5's chain is platform ∩ workspace ∩ …, and a request naming no
    workspace is measured against the platform alone.

    Concretely: an installation that approved `open.feishu.cn` at the
    platform layer would deliver replies for a workspace that never
    approved it. The workspace layer would still be in the database, still
    be shown in the console, and mean nothing — which is the shape of
    control this project keeps shipping by accident.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)
    await _finish(engine, run_id, said="做完了")

    sender = _Sender()
    await _dispatch(engine, sender)

    assert set(sender.asked_for) == {UUID(workspace_id)}


async def test_every_retry_of_one_reply_carries_the_same_deduplication_key(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry bound is not a delivery guarantee, and never was.

    A failure after the request left this process is indistinguishable from
    one before it — the platform cannot tell whether Feishu acted on it. A
    live tenant proved what that costs: five retries, five real messages,
    one of which the person had asked for.

    So the key is the delivery's own row id, stable across every attempt, and
    Feishu settles the question on its side. A key generated per attempt
    would look like deduplication in the code and deduplicate nothing.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)
    await _finish(engine, run_id, said="做完了")

    sender = _Sender(refusing=FeishuApiRefused(0, "read failed after sending"))
    for _ in range(3):
        await _dispatch(engine, sender, max_attempts=3)

    keys = {attempt["delivery_key"] for attempt in sender.after_opening()}
    assert len(sender.after_opening()) == 3
    assert len(keys) == 1
    assert "" not in keys


async def _block_head(engine: AsyncEngine, run_id: UUID) -> None:
    """Put the Session's head Run into `paused`, the way a person pausing it
    from the console would. Everything after this arrives behind it."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE runs SET status = 'paused', pause_reason = 'manual'"
                " WHERE id = :r"
            ),
            {"r": run_id},
        )


async def test_a_queued_message_is_told_it_is_queued(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§19.2's `不能静默吞入新消息`, which was false until now.

    Every fact §497 requires was computed on the inbound path and written
    into the webhook's HTTP response — a body Feishu's server discards the
    moment it reads the 200. The person who sent the message saw an empty
    chat, which is what a dropped message also looks like.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    first = _deliver(client, binding_id, event_id="om_head")
    await _block_head(engine, first)

    _deliver(client, binding_id, event_id="om_queued")

    sender = _Sender()
    await _dispatch(engine, sender)

    queued = [e for e in sender.sent if "排队" in e["text"]]
    assert len(queued) == 1
    assert queued[0]["open_id"] == "ou_zhang"


async def test_the_queue_notice_is_sent_once(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan runs every second for as long as the head stays paused. A
    notice without its own stamp would be re-sent every one of them."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    first = _deliver(client, binding_id, event_id="om_head")
    await _block_head(engine, first)
    _deliver(client, binding_id, event_id="om_queued")

    sender = _Sender()
    for _ in range(3):
        await _dispatch(engine, sender)

    # The queue card *is* the opening card now — a blocked message opens
    # with it rather than with 「正在处理」, which would be false. So this
    # asserts the delivery opened once, however many times the scan ran.
    opened = [e for e in sender.sent if e["delivery_key"].endswith(":c")]
    assert len([e for e in opened if "排队" in e["text"]]) == 1


async def test_a_message_that_was_never_queued_gets_no_notice(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary case. A card saying "you are queued" in front of
    somebody who was not queued is noise, and noise in a chat is the thing
    that makes people stop reading the cards that matter."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)
    await _finish(engine, run_id, said="做完了")

    sender = _Sender()
    await _dispatch(engine, sender)

    assert [e for e in sender.after_opening() if e["kind"] == "card"] == []


async def test_a_queued_message_still_gets_its_answer_afterwards(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sends for one delivery, and the notice must not consume the reply.

    A single `replied_at` could not carry both — stamping it for the queue
    notice would settle the row, and the answer the person is actually
    waiting for would never be sent. That is why the notice has its own
    stamp rather than sharing one.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    first = _deliver(client, binding_id, event_id="om_head")
    await _block_head(engine, first)
    queued = _deliver(client, binding_id, event_id="om_queued")

    sender = _Sender()
    await _dispatch(engine, sender)
    await _finish(engine, queued, said="终于轮到我了")
    await _dispatch(engine, sender)

    # Opened as a queue card, then rewritten in place with the answer — one
    # message in the conversation, not two. The notice must not settle the
    # delivery, which is what this test has always been about.
    said = [e["text"] for e in sender.sent]
    assert any("排队" in text for text in said)
    assert any("终于轮到我了" in text for text in said)


async def test_the_notice_and_the_reply_do_not_share_a_deduplication_key(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both are sends for one `channel_events` row. Sharing the key would
    make Feishu's own deduplication swallow the second one — the reply — and
    the person would keep the queue notice and never get the answer."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    first = _deliver(client, binding_id, event_id="om_head")
    await _block_head(engine, first)
    queued = _deliver(client, binding_id, event_id="om_queued")

    sender = _Sender()
    await _dispatch(engine, sender)
    await _finish(engine, queued, said="终于轮到我了")
    await _dispatch(engine, sender)

    # Only creating a message needs a key; a patch is idempotent by
    # construction and carries none. What must still not collide is the
    # opening card against a fallback message — covered by
    # `test_a_card_that_cannot_be_updated_falls_back_to_a_new_message`.
    keys = [e["delivery_key"] for e in sender.sent if e["delivery_key"]]
    assert len(set(keys)) == len(keys)


def _photo(event_id: str = "om_photo") -> dict[str, Any]:
    """A voice note, despite the name this helper kept.

    It was an image until images became readable. What these tests are
    about is the *refusal* path — a message with a sender that this build
    cannot read — and that property now belongs to audio.
    """
    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_zhang"}},
            "message": {
                "message_type": "audio",
                "content": json.dumps({"file_key": "f_v2_1"}),
            },
        },
    }


def _deliver_raw(
    client: TestClient, binding_id: UUID, envelope: dict[str, Any]
) -> Any:
    body = _encrypt(envelope)
    return client.post(
        f"/api/v1/channels/feishu/{binding_id}/webhook",
        content=body,
        headers=_headers(body),
    )


async def test_an_unreadable_message_gets_an_answer_instead_of_silence(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§19.2's `不能静默吞入新消息`, on the path that never honoured it.

    An unreadable message produced a 200 and a log line. The comment on that
    branch said `never silently` — true of the platform's records, false of
    the person who sent the photo and got nothing.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)

    posted = _deliver_raw(client, binding_id, _photo())
    assert posted.status_code == 200, posted.text

    sender = _Sender()
    await _dispatch(engine, sender)

    assert len(sender.after_opening()) == 1
    assert sender.after_opening()[0]["open_id"] == "ou_zhang"
    assert "文字" in sender.after_opening()[0]["text"]


async def test_an_unreadable_message_starts_no_run(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Answering is not the same as understanding. Nothing is submitted to
    an Agent, because there is nothing this build could hand it."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)

    _deliver_raw(client, binding_id, _photo())

    async with engine.connect() as connection:
        runs = await connection.execute(text("SELECT count(*) FROM runs"))
    assert runs.scalar_one() == 0


async def test_the_same_unreadable_message_twice_is_answered_once(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§574's claim has to cover this path too.

    Feishu delivers at-least-once. Without a claim, a refusal would be sent
    for every retry — so the person who sent one photo would be told four
    times that photos are not supported, which is a worse failure than the
    silence it replaces.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)

    _deliver_raw(client, binding_id, _photo())
    _deliver_raw(client, binding_id, _photo())

    sender = _Sender()
    await _dispatch(engine, sender)
    await _dispatch(engine, sender)

    assert len(sender.after_opening()) == 1


async def test_a_broken_envelope_is_answered_to_nobody(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sender in the envelope means no recipient for a refusal. Guessing
    one would send somebody else's inbox an explanation for a message they
    never sent."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)

    posted = _deliver_raw(
        client,
        binding_id,
        {
            "header": {"event_id": "om_broken"},
            "event": {"message": {"message_type": "image", "content": "{}"}},
        },
    )
    assert posted.status_code == 200, posted.text

    sender = _Sender()
    await _dispatch(engine, sender)

    assert sender.after_opening() == []


async def _age_delivery(engine: AsyncEngine, seconds: int) -> None:
    """Backdate every delivery, so "it has been a while" is true without
    the test having to wait it out."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE channel_events"
                " SET received_at = received_at - make_interval(secs => :s)"
            ),
            {"s": seconds},
        )


async def test_a_slow_run_says_it_is_still_working(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§19.2's `流式能力受渠道限制时的进度更新`.

    Feishu has no streaming, so a Run that takes two minutes shows the
    person nothing at all. They conclude it broke and send the message
    again — which is the same conclusion, and the same second message, that
    silence produced on every other path in this module.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    _deliver(client, binding_id)
    await _age_delivery(engine, 60)

    sender = _Sender()
    await _dispatch(engine, sender)

    assert len(sender.after_opening()) == 1
    # The recipient is fixed by the opening card; a patch names none — the
    # message id already decides who sees it.
    assert sender.opening["open_id"] == "ou_zhang"
    assert "还在" in sender.after_opening()[0]["text"]


async def test_a_run_that_has_only_just_started_gets_no_progress_note(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No *progress* note. The delivery's opening card is already there —
    that is unconditional now — and this asserts nothing follows it.

    Renamed from "says nothing", which stopped being true: the card design
    means something is said within a second of every message. What must not
    happen is a second thing arriving for an ordinary Run.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    _deliver(client, binding_id)

    sender = _Sender()
    await _dispatch(engine, sender)

    assert sender.after_opening() == []


async def test_a_run_that_already_finished_gets_its_answer_not_a_progress_note(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow *and* done by the time the scan looked. Telling somebody "still
    working" and then immediately answering reads as a platform talking to
    itself."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)
    await _age_delivery(engine, 60)
    await _finish(engine, run_id, said="慢是慢,做完了")

    sender = _Sender()
    await _dispatch(engine, sender)

    assert len(sender.after_opening()) == 1
    assert "慢是慢,做完了" in sender.after_opening()[0]["text"]


async def test_the_progress_note_is_sent_once_however_long_it_runs(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliberately once, not once per interval.

    A ten-minute Run must not produce thirty "still working" messages. This
    build does not do step-by-step progress at all: what a Run is doing is
    tool names and internal state, which §19.1 keeps off an end-user
    surface, and a chat that scrolls itself is worse than one that waits.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    _deliver(client, binding_id)
    await _age_delivery(engine, 60)

    sender = _Sender()
    for _ in range(4):
        await _dispatch(engine, sender)

    assert len(sender.after_opening()) == 1


async def test_a_queued_message_gets_the_card_and_not_a_progress_note(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Still working" would be false — nothing is working on it yet, it is
    queued — and the card already told them that, with the reason."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    first = _deliver(client, binding_id, event_id="om_head")
    await _block_head(engine, first)
    _deliver(client, binding_id, event_id="om_queued")
    await _age_delivery(engine, 60)

    sender = _Sender()
    await _dispatch(engine, sender)

    # Two deliveries here — the head and the one behind it — so this looks
    # across every opening card rather than at the first. The queued one
    # opened as a queue card, and nothing follows either: 「还在处理」 would
    # be false for a message nothing is processing.
    opened = [e["text"] for e in sender.sent if e["delivery_key"].endswith(":c")]
    assert any("排队" in text for text in opened)
    assert sender.after_opening() == []


async def test_a_slow_run_still_delivers_its_answer_afterwards(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The progress note must not settle the delivery. Same failure the
    queue notice would have had with a shared stamp: told it is coming,
    then never told what it was."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)
    await _age_delivery(engine, 60)

    sender = _Sender()
    await _dispatch(engine, sender)
    await _finish(engine, run_id, said="终于好了")
    await _dispatch(engine, sender)

    assert len(sender.after_opening()) == 2
    assert "还在" in sender.after_opening()[0]["text"]
    assert "终于好了" in sender.after_opening()[1]["text"]
    # Two writes to one message rather than two messages, so neither
    # carries a key — the progress note cannot consume the answer.
    assert [e["kind"] for e in sender.after_opening()] == ["patch", "patch"]


async def test_a_run_of_a_dozen_seconds_is_slow_enough_to_mention(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The threshold, pinned by what a person called "a long time".

    Measured against the live tenant: a message asking the Agent to create
    ten files and count words took **17.7 seconds**, and the person who sent
    it said it felt like a long wait with nothing happening. The first
    threshold was 20 seconds, chosen from a sample of ordinary Runs that
    took about five — so the one case that actually needed the notice was
    the one case that missed it, by 2.3 seconds.

    Asserted at 12 seconds rather than by reading the constant: a test that
    imports the number cannot fail when the number is wrong, and the number
    being wrong is exactly what happened.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    _deliver(client, binding_id)
    await _age_delivery(engine, 12)

    sender = _Sender()
    await _dispatch(engine, sender)

    assert len(sender.after_opening()) == 1
    assert "还在" in sender.after_opening()[0]["text"]


async def test_an_ordinary_five_second_run_is_still_left_alone(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same judgement, and the reason the threshold
    cannot simply go to zero. An ordinary Feishu reply measures about five
    seconds end to end; a notice on every one of those is noise, and noise
    is what stops people reading the messages that matter."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    _deliver(client, binding_id)
    await _age_delivery(engine, 5)

    sender = _Sender()
    await _dispatch(engine, sender)

    assert sender.after_opening() == []


# --- one card per delivery, rewritten in place ---


async def test_a_message_gets_a_card_before_anything_is_known(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the whole design: something appears immediately.

    Not after 8 seconds, and not after the Run finishes. A text message
    cannot be taken back, so the platform used to have to know what to say
    before saying anything — and the wait is indistinguishable from a
    dropped message.

    Sent by the scan rather than on the inbound path, deliberately: sending
    inside the webhook transaction would make an inbound delivery depend on
    `open.feishu.cn` being reachable, and a timeout would leave Feishu
    retrying a delivery whose claim was already taken. The scan runs every
    second, which is what "immediately" means here.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    _deliver(client, binding_id)

    sender = _Sender()
    await _dispatch(engine, sender)

    assert len(sender.sent) == 1
    assert sender.sent[0]["kind"] == "card"
    assert "收到" in sender.sent[0]["text"] or "处理" in sender.sent[0]["text"]


async def test_the_card_id_is_remembered_so_it_can_be_rewritten(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feishu's `message_id` is the only handle an update accepts. Losing it
    turns every later stage back into a separate message."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    _deliver(client, binding_id)

    await _dispatch(engine, _Sender())

    async with engine.connect() as connection:
        stored = await connection.execute(
            text("SELECT card_message_id FROM channel_events")
        )
    assert stored.scalar_one() == "om_card"


async def test_the_answer_rewrites_the_card_instead_of_sending_a_new_message(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One card that changes, not a conversation full of status updates."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)

    sender = _Sender()
    await _dispatch(engine, sender)
    await _finish(engine, run_id, said="上周有 12 单。")
    await _dispatch(engine, sender)

    assert [entry["kind"] for entry in sender.sent] == ["card", "patch"]
    assert sender.sent[1]["message_id"] == "om_card"
    assert "上周有 12 单。" in sender.sent[1]["text"]


async def test_a_card_that_cannot_be_updated_falls_back_to_a_new_message(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feishu answered the send without a `message_id`, so there is nothing
    to patch. The answer still has to arrive — a design that could only
    speak through a card would go silent whenever the card was unreachable,
    which is worse than the two-message version it replaces.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)

    sender = _Sender(card_id=None)
    await _dispatch(engine, sender)
    await _finish(engine, run_id, said="上周有 12 单。")
    await _dispatch(engine, sender)

    assert [entry["kind"] for entry in sender.sent] == ["card", "text"]
    assert "上周有 12 单。" in sender.sent[1]["text"]


async def test_a_slow_run_rewrites_the_card_rather_than_adding_a_message(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    _deliver(client, binding_id)

    sender = _Sender()
    await _dispatch(engine, sender)
    await _age_delivery(engine, 12)
    await _dispatch(engine, sender)

    assert [entry["kind"] for entry in sender.sent] == ["card", "patch"]
    assert "还在" in sender.sent[1]["text"]


async def test_a_queued_message_opens_with_the_queue_card_directly(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No "正在处理" first. Nothing is processing it — it is queued, and
    saying otherwise would be a sentence the platform knows to be false."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    first = _deliver(client, binding_id, event_id="om_head")
    await _block_head(engine, first)
    _deliver(client, binding_id, event_id="om_queued")

    sender = _Sender()
    await _dispatch(engine, sender)

    queued = [e for e in sender.sent if "排队" in e["text"]]
    assert len(queued) == 1
    assert queued[0]["kind"] == "card"


async def test_a_failed_run_rewrites_the_card_with_the_reason(
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

    sender = _Sender()
    await _dispatch(engine, sender)
    await _finish(engine, run_id, said="", status="failed")
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET checkpoint = :c WHERE id = :r"),
            {"r": run_id, "c": json.dumps({"failure": "model_endpoint_unreachable"})},
        )
    await _dispatch(engine, sender)

    assert sender.sent[1]["kind"] == "patch"
    assert "model_endpoint_unreachable" in sender.sent[1]["text"]


async def test_a_compaction_that_had_nothing_to_gain_says_so_to_the_person(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/compact` 压不动的时候，那句话要真的到达发消息的人。

    Worker 那一侧已经有测试证明这条事件会被写出来
    （`runs/test_compaction_threshold.py`），`reply_for` 也有单元测试证明措辞。
    这条补的是中间那一段——`pending_replies` 去读它、dispatcher 把它传下去。
    **这一段之前一条测试都没有**：`compaction` 那条链是靠一次真机走查才知道通
    的，而这条新的连那个都没有。

    判据是发出去的那句话，不是事件行在不在：这个仓库已经因为「写进去了没人
    够得着」栽过至少五次，而每一次后端测试都是全绿的。
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    monkeypatch.setenv(SECRET_ENV, "s3cret")
    binding_id = await _binding(engine, workspace_id, published_agent)
    run_id = _deliver(client, binding_id)

    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET purpose = 'compaction' WHERE id = :r"),
            {"r": run_id},
        )
        await connection.execute(
            text(
                # 序号接着已有事件往下取：`run_created` 已经占了 1，而
                # `(run_id, sequence)` 是唯一的。
                "INSERT INTO run_events"
                " (id, run_id, workspace_id, sequence, event_type, payload, occurred_at)"
                " SELECT gen_random_uuid(), :r, r.workspace_id,"
                "  (SELECT coalesce(max(sequence), 0) + 1 FROM run_events"
                "    WHERE run_id = :r),"
                " 'context_compaction_skipped', :p, now() FROM runs r WHERE r.id = :r"
            ),
            {"r": run_id, "p": json.dumps({"reason": "no_gain", "covered": 2})},
        )
    # 压缩 Run 不回答问题，所以没有 assistant 那一条消息可引。
    await _finish(engine, run_id, said="")

    sender = _Sender()
    await _dispatch(engine, sender)

    sent = sender.after_opening()
    assert len(sent) == 1, "压不动这件事没有任何东西发给发消息的人"
    assert "没有压缩" in sent[0]["text"], sent[0]["text"]
    # 「稍后再试一次」是给「压缩失败」那一种的：这一种再试同样不会成。
    assert "再试" not in sent[0]["text"], sent[0]["text"]
