"""Two transports, one event — and the crypto that lets the second exist.

§929 makes Webhook the production fallback for the WebSocket long
connection, because M1's laboratory note left the disconnect-replay
question unmeasured and §19.2 says a channel that can lose events needs the
fallback. A fallback that took its own code path would be a second
implementation nobody exercises until the day the first one fails, so the
claim these tests exist to hold is that both transports converge before
anything downstream sees them.
"""

import base64
import hashlib
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from tiny_hermes.channels.domain.events import MalformedChannelEvent
from tiny_hermes.channels.domain.feishu import event_from_envelope
from tiny_hermes.channels.domain.webhook import (
    WebhookRefused,
    decrypt_payload,
    verify_signature,
)

ENCRYPT_KEY = "a-tenant-encrypt-key"

ENVELOPE: dict[str, Any] = {
    "schema": "2.0",
    "header": {"event_id": "om_event_1", "event_type": "im.message.receive_v1"},
    "event": {
        "sender": {"sender_id": {"open_id": "ou_zhang"}},
        "message": {
            "message_id": "om_msg_1",
            "message_type": "text",
            "content": json.dumps({"text": "帮我查一下上周的订单"}),
        },
    },
}


def _encrypt(envelope: dict[str, Any], key: str = ENCRYPT_KEY) -> bytes:
    """Feishu's side of the wire, so the test decrypts something a tenant
    would really have sent rather than something this module produced for
    its own convenience."""
    plaintext = json.dumps(envelope).encode()
    pad = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad]) * pad
    iv = b"0123456789abcdef"
    aes_key = hashlib.sha256(key.encode()).digest()
    encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    blob = iv + encryptor.update(padded) + encryptor.finalize()
    return json.dumps({"encrypt": base64.b64encode(blob).decode()}).encode()


def _sign(body: bytes, timestamp: str = "1755830400", nonce: str = "n1") -> str:
    return hashlib.sha256(
        timestamp.encode() + nonce.encode() + ENCRYPT_KEY.encode() + body
    ).hexdigest()


def test_both_transports_produce_the_same_event() -> None:
    """The claim "one ingestion path" rests on, asserted rather than assumed.

    The WebSocket transport is handed the envelope already decrypted by the
    vendor SDK; the Webhook transport decrypts it here. If these two ever
    diverge — a field read in one and not the other, a schema version
    accepted by one — deduplication and Run creation would quietly behave
    differently depending on how a tenant happened to be connected.
    """
    from_websocket = event_from_envelope(ENVELOPE)

    body = _encrypt(ENVELOPE)
    verify_signature(
        timestamp="1755830400",
        nonce="n1",
        encrypt_key=ENCRYPT_KEY,
        body=body,
        signature=_sign(body),
    )
    from_webhook = event_from_envelope(decrypt_payload(encrypt_key=ENCRYPT_KEY, body=body))

    assert from_websocket == from_webhook
    assert from_webhook.channel_event_id == "om_event_1"
    assert from_webhook.external_user_id == "ou_zhang"
    assert from_webhook.text == "帮我查一下上周的订单"


def test_the_v1_envelope_still_yields_an_event_id() -> None:
    """A tenant chooses its schema version, so refusing v1 would be a
    deployment-time surprise rather than a platform decision. Only the id
    moved; §574's key needs nothing else from the header."""
    v1: dict[str, Any] = {
        "uuid": "legacy_uuid_1",
        "event": ENVELOPE["event"],
    }

    assert event_from_envelope(v1).channel_event_id == "legacy_uuid_1"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"header": {}}, "no event id"),
        ({"event": {"sender": {}, "message": {}}}, "no sender"),
    ],
)
def test_a_payload_missing_what_the_key_needs_is_refused(
    mutation: dict[str, Any], reason: str
) -> None:
    """Refused, not partially accepted. An event with no id cannot be
    deduplicated and one with no sender cannot be attributed — either would
    create a Run nobody can trace back to a person."""
    del reason
    with pytest.raises(MalformedChannelEvent):
        event_from_envelope({**ENVELOPE, **mutation})


def test_a_signature_from_a_different_key_is_refused() -> None:
    """The whole point of the Webhook transport's front door: this endpoint
    is reachable by anyone who learns the URL, so the signature is the only
    thing separating Feishu from the internet."""
    body = _encrypt(ENVELOPE)
    forged = hashlib.sha256(
        b"1755830400" + b"n1" + b"a-different-tenants-key" + body
    ).hexdigest()

    with pytest.raises(WebhookRefused):
        verify_signature(
            timestamp="1755830400",
            nonce="n1",
            encrypt_key=ENCRYPT_KEY,
            body=body,
            signature=forged,
        )


def test_a_body_altered_after_signing_is_refused() -> None:
    """The body is inside the signature, which is also why no replay window
    is enforced: a replayed request is byte-identical, so it carries the
    same event id, and §574's claim refuses it there."""
    body = _encrypt(ENVELOPE)
    signature = _sign(body)
    tampered = _encrypt({**ENVELOPE, "header": {"event_id": "om_event_2"}})

    with pytest.raises(WebhookRefused):
        verify_signature(
            timestamp="1755830400",
            nonce="n1",
            encrypt_key=ENCRYPT_KEY,
            body=tampered,
            signature=signature,
        )


def test_a_payload_encrypted_with_another_key_is_refused_not_garbled() -> None:
    """AES-CBC with the wrong key decrypts to noise rather than failing, so
    the refusal has to come from the padding check and the JSON parse. A
    version that returned the noise would hand malformed bytes to the
    envelope reader and report a schema problem instead of a key problem."""
    body = _encrypt(ENVELOPE, key="some-other-tenants-key")

    with pytest.raises(WebhookRefused):
        decrypt_payload(encrypt_key=ENCRYPT_KEY, body=body)


def test_a_truncated_ciphertext_is_refused() -> None:
    body = _encrypt(ENVELOPE)
    outer = json.loads(body)
    blob = base64.b64decode(outer["encrypt"])
    outer["encrypt"] = base64.b64encode(blob[:20]).decode()

    with pytest.raises(WebhookRefused):
        decrypt_payload(encrypt_key=ENCRYPT_KEY, body=json.dumps(outer).encode())
