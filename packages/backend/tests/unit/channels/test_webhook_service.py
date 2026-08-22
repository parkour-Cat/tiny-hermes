"""The order the checks happen in, and the handshake without which this
endpoint cannot be configured at all.
"""

import base64
import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from tiny_hermes.channels.application.webhook_service import (
    BindingSecrets,
    Challenge,
    Claimed,
    FeishuWebhookService,
)
from tiny_hermes.channels.domain.events import MalformedChannelEvent
from tiny_hermes.channels.domain.webhook import WebhookRefused

KEY = "tenant-key"
BINDING = uuid4()


class SpyClaims:
    def __init__(self, answer: UUID | None = None) -> None:
        self.calls: list[tuple[UUID, str]] = []
        self._answer = answer if answer is not None else uuid4()
        self.duplicate = False

    async def claim_delivery(
        self, binding_id: UUID, channel_event_id: str, now: datetime
    ) -> UUID | None:
        del now
        self.calls.append((binding_id, channel_event_id))
        return None if self.duplicate else self._answer


def _body(envelope: dict[str, Any], key: str = KEY) -> bytes:
    plaintext = json.dumps(envelope).encode()
    pad = 16 - (len(plaintext) % 16)
    iv = b"0123456789abcdef"
    encryptor = Cipher(
        algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(iv)
    ).encryptor()
    blob = iv + encryptor.update(plaintext + bytes([pad]) * pad) + encryptor.finalize()
    return json.dumps({"encrypt": base64.b64encode(blob).decode()}).encode()


def _sig(body: bytes) -> str:
    return hashlib.sha256(b"1755830400" + b"n1" + KEY.encode() + body).hexdigest()


def _message() -> dict[str, Any]:
    return {
        "header": {"event_id": "om_1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_zhang"}},
            "message": {"content": json.dumps({"text": "hello"})},
        },
    }


async def _accept(service: FeishuWebhookService, body: bytes, signature: str | None = None):
    return await service.accept(
        secrets=BindingSecrets(binding_id=BINDING, encrypt_key=KEY),
        body=body,
        timestamp="1755830400",
        nonce="n1",
        signature=signature if signature is not None else _sig(body),
    )


async def test_the_registration_challenge_is_answered() -> None:
    """Without this a webhook cannot be configured at all — Feishu will not
    save a callback address that fails the handshake. An endpoint that only
    handled events would be one nobody could turn on."""
    claims = SpyClaims()
    body = _body({"type": "url_verification", "challenge": "c-123"})

    answer = await _accept(FeishuWebhookService(claims), body)

    assert answer == Challenge(challenge="c-123")
    # And it is not mistaken for an event: nothing was claimed.
    assert claims.calls == []


async def test_an_unsigned_body_is_never_decrypted() -> None:
    """Order matters more than the individual checks. Decrypting first
    would run this platform's cipher over an attacker's bytes, and
    normalizing first would let them pick an `event_id` and suppress a real
    delivery by claiming it before Feishu's arrives."""
    claims = SpyClaims()
    body = _body(_message())

    with pytest.raises(WebhookRefused):
        await _accept(FeishuWebhookService(claims), body, signature="0" * 64)

    assert claims.calls == []


async def test_a_verified_message_is_claimed_once() -> None:
    claims = SpyClaims()

    answer = await _accept(FeishuWebhookService(claims), _body(_message()))

    assert isinstance(answer, Claimed)
    assert answer.claim_id is not None
    assert answer.event.external_user_id == "ou_zhang"
    assert claims.calls == [(BINDING, "om_1")]


async def test_a_duplicate_delivery_is_reported_rather_than_raised() -> None:
    """Feishu delivers at-least-once, so a duplicate is ordinary traffic.
    Raising here would turn a 200 into a 500 and Feishu would retry the
    same event on its schedule — the endpoint would punish itself for
    working correctly."""
    claims = SpyClaims()
    claims.duplicate = True

    answer = await _accept(FeishuWebhookService(claims), _body(_message()))

    assert isinstance(answer, Claimed)
    assert answer.claim_id is None


async def test_a_verified_payload_this_platform_cannot_read_is_not_a_refusal() -> None:
    """The sender proved it was Feishu, so an unreadable payload is a
    message type this platform does not handle yet — not an intruder. The
    two must stay distinguishable, because one is a reason to look at logs
    and the other is a reason to look at an attacker."""
    claims = SpyClaims()
    body = _body({"header": {"event_id": "om_2"}, "event": {"message": {}}})

    with pytest.raises(MalformedChannelEvent):
        await _accept(FeishuWebhookService(claims), body)

    assert claims.calls == []
