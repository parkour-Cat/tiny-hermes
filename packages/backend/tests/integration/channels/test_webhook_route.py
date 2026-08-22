"""The Feishu webhook, reached the way Feishu reaches it.

Everything under this route was already tested without a tenant. What none
of those tests could show is that the door exists — that a POST from
outside, carrying nothing but a signature, turns into a Run owned by the
person who sent the message. That gap is the one this repository has
produced five times and named in its own records, so it gets a test that
goes in through HTTP rather than through a service call.
"""

import base64
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

KEY = "tenant-encrypt-key"


def _encrypt(envelope: dict[str, Any], key: str = KEY) -> bytes:
    plaintext = json.dumps(envelope).encode()
    pad = 16 - (len(plaintext) % 16)
    iv = b"0123456789abcdef"
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    encryptor = Cipher(
        algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(iv)
    ).encryptor()
    blob = iv + encryptor.update(plaintext + bytes([pad]) * pad) + encryptor.finalize()
    return json.dumps({"encrypt": base64.b64encode(blob).decode()}).encode()


def _headers(body: bytes, key: str = KEY) -> dict[str, str]:
    signature = hashlib.sha256(b"1755830400" + b"n1" + key.encode() + body).hexdigest()
    return {
        "X-Lark-Request-Timestamp": "1755830400",
        "X-Lark-Request-Nonce": "n1",
        "X-Lark-Signature": signature,
        "Content-Type": "application/json",
    }


def _message(event_id: str = "om_1", text_body: str = "查一下上周的订单") -> dict[str, Any]:
    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_zhang"}},
            "message": {"content": json.dumps({"text": text_body})},
        },
    }


async def _binding(engine: AsyncEngine, workspace_id: str, agent_id: str) -> UUID:
    binding_id = uuid4()
    async with engine.begin() as connection:
        owner = await connection.execute(text("SELECT id FROM users LIMIT 1"))
        await connection.execute(
            text(
                "INSERT INTO channel_bindings"
                " (id, workspace_id, channel, agent_id, status, created_by,"
                "  created_at, encrypt_key_ref)"
                " VALUES (:i, :w, 'feishu', :a, 'active', :u, now(), :k)"
            ),
            {
                "i": binding_id,
                "w": UUID(workspace_id),
                "a": UUID(agent_id),
                "u": owner.scalar_one(),
                "k": "FEISHU_TEST_KEY",
            },
        )
    return binding_id


async def test_a_signed_message_becomes_a_run_owned_by_its_sender(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: Any,
) -> None:
    """The whole point of this route, end to end over HTTP.

    §122: the sender does not become a workspace member. The Run's Session
    is owned by an `EndUser` resolved through `external_identities`, which
    is asserted from the database rather than from the response — the
    response could say anything, and what matters is who the platform
    believes this work belongs to.
    """
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    binding_id = await _binding(engine, workspace_id, published_agent)
    body = _encrypt(_message())

    posted = client.post(
        f"/api/v1/channels/feishu/{binding_id}/webhook",
        content=body,
        headers=_headers(body),
    )

    assert posted.status_code == 200, posted.text
    run_id = posted.json()["run_id"]
    assert run_id is not None

    async with engine.connect() as connection:
        owner = await connection.execute(
            text(
                "SELECT s.caller_type, s.caller_id FROM runs r"
                " JOIN sessions s ON s.id = r.session_id WHERE r.id = :r"
            ),
            {"r": UUID(run_id)},
        )
        caller_type, caller_id = owner.one()
        mapped = await connection.execute(
            text(
                "SELECT end_user_id FROM external_identities"
                " WHERE workspace_id = :w AND channel = 'feishu'"
                " AND external_user_id = 'ou_zhang'"
            ),
            {"w": UUID(workspace_id)},
        )

    assert caller_type == "end_user"
    assert caller_id == mapped.scalar_one()


async def test_the_registration_handshake_is_answered_over_http(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: Any,
) -> None:
    """Feishu will not save a callback address whose handshake fails, so
    without this the endpoint could never be configured and everything
    behind it would be unreachable in a way no unit test would show."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    binding_id = await _binding(engine, workspace_id, published_agent)
    body = _encrypt({"type": "url_verification", "challenge": "c-abc"})

    posted = client.post(
        f"/api/v1/channels/feishu/{binding_id}/webhook",
        content=body,
        headers=_headers(body),
    )

    assert posted.status_code == 200, posted.text
    assert posted.json()["challenge"] == "c-abc"


async def test_a_forged_signature_is_refused_and_starts_nothing(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: Any,
) -> None:
    """This endpoint is reachable by anyone who learns the URL. The
    signature is the only thing between it and the internet, so the refusal
    is asserted together with "no Run exists" — a 401 that had already
    started work would be a 401 in name only."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    binding_id = await _binding(engine, workspace_id, published_agent)
    body = _encrypt(_message(event_id="om_forged"))

    posted = client.post(
        f"/api/v1/channels/feishu/{binding_id}/webhook",
        content=body,
        headers=_headers(body, key="a-different-key"),
    )

    assert posted.status_code == 401, posted.text
    async with engine.connect() as connection:
        claimed = await connection.execute(
            text("SELECT count(*) FROM channel_events WHERE channel_binding_id = :b"),
            {"b": binding_id},
        )
    assert claimed.scalar_one() == 0


async def test_the_same_delivery_twice_starts_one_run(
    client: TestClient,
    engine: AsyncEngine,
    workspace_id: str,
    published_agent: str,
    monkeypatch: Any,
) -> None:
    """§574 over HTTP. Feishu retries on a schedule, so the second delivery
    is ordinary traffic: it must answer 200 — anything else has Feishu
    retrying for six hours — and must not start a second Run."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    binding_id = await _binding(engine, workspace_id, published_agent)
    body = _encrypt(_message(event_id="om_retried"))
    headers = _headers(body)
    url = f"/api/v1/channels/feishu/{binding_id}/webhook"

    first = client.post(url, content=body, headers=headers)
    second = client.post(url, content=body, headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["run_id"] is not None
    assert second.json()["run_id"] is None
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT count(*) FROM channel_events WHERE channel_binding_id = :b"),
            {"b": binding_id},
        )
    assert rows.scalar_one() == 1


async def test_an_unknown_binding_looks_exactly_like_a_bad_signature(
    client: TestClient, monkeypatch: Any
) -> None:
    """Telling them apart would let anyone holding a URL enumerate which
    bindings exist and which are switched off."""
    monkeypatch.setenv("FEISHU_TEST_KEY", KEY)
    body = _encrypt(_message())

    posted = client.post(
        f"/api/v1/channels/feishu/{uuid4()}/webhook",
        content=body,
        headers=_headers(body),
    )

    assert posted.status_code == 401, posted.text
