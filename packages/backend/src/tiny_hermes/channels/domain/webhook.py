"""Feishu's Webhook transport: prove it came from Feishu, then read it.

§929 requires this alongside the WebSocket long connection, because M1's
laboratory note left the disconnect-replay question unmeasured and §19.2
says Webhook becomes the mandatory production fallback if events can be
lost while nothing is connected. A fallback nobody can turn on is not one.

Algorithm per Feishu's own encryption/signature documentation:

- signature: `sha256(timestamp + nonce + encrypt_key + raw_body)`, hex,
  compared against `X-Lark-Signature`
- payload: `{"encrypt": "<base64>"}`, AES-256-CBC, key `sha256(encrypt_key)`,
  IV the first 16 bytes of the decoded ciphertext, PKCS7 padding

Nothing here was written from memory — getting either wrong is a hole, not
a bug.
"""

import base64
import hashlib
import hmac
import json
from typing import Any, cast

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK_SIZE = 16


class WebhookRefused(Exception):
    """The request did not prove it came from Feishu, or did not decrypt.

    One exception for both, deliberately: telling a caller *which* check it
    failed tells an attacker whether a forged signature was well-formed,
    and the answer to either is the same refusal.
    """


def verify_signature(
    *, timestamp: str, nonce: str, encrypt_key: str, body: bytes, signature: str
) -> None:
    """Refuse anything that is not signed with this binding's own key.

    `compare_digest` rather than `==`: a byte-by-byte comparison leaks, in
    its timing, how much of a forged signature was correct, which is enough
    to construct one a byte at a time.

    No replay window is enforced here, and that is a decision rather than an
    omission. The body is inside the signature, so a replayed request is
    byte-identical to the original — which means it carries the same
    `event_id`, and §574's claim (`channel_events`) already refuses it. A
    timestamp window would add a second, weaker guard against something the
    deduplication key settles exactly.
    """
    digest = hashlib.sha256(
        timestamp.encode() + nonce.encode() + encrypt_key.encode() + body
    ).hexdigest()
    if not hmac.compare_digest(digest, signature):
        raise WebhookRefused("signature does not match")


def decrypt_payload(*, encrypt_key: str, body: bytes) -> dict[str, Any]:
    """`{"encrypt": ...}` in, the event envelope out."""
    try:
        outer: object = json.loads(body)
    except json.JSONDecodeError as error:
        raise WebhookRefused("body is not JSON") from error
    if not isinstance(outer, dict):
        raise WebhookRefused("body is not an object")
    encoded: object = cast(dict[str, Any], outer).get("encrypt")
    if not isinstance(encoded, str):
        raise WebhookRefused("body carries no encrypt field")

    try:
        blob = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise WebhookRefused("encrypt field is not base64") from error
    if len(blob) <= BLOCK_SIZE or (len(blob) - BLOCK_SIZE) % BLOCK_SIZE != 0:
        raise WebhookRefused("ciphertext is not a whole number of blocks")

    key = hashlib.sha256(encrypt_key.encode()).digest()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(blob[:BLOCK_SIZE])).decryptor()
    padded = decryptor.update(blob[BLOCK_SIZE:]) + decryptor.finalize()

    # PKCS7, unpadded by hand because a wrong final byte must be a refusal
    # rather than a slice that silently returns the wrong bytes.
    pad = padded[-1]
    if pad < 1 or pad > BLOCK_SIZE or len(padded) < pad:
        raise WebhookRefused("bad padding")
    if padded[-pad:] != bytes([pad]) * pad:
        raise WebhookRefused("bad padding")
    plaintext = padded[:-pad]

    try:
        envelope: object = json.loads(plaintext)
    except json.JSONDecodeError as error:
        raise WebhookRefused("decrypted payload is not JSON") from error
    if not isinstance(envelope, dict):
        raise WebhookRefused("decrypted payload is not an object")
    return cast(dict[str, Any], envelope)
