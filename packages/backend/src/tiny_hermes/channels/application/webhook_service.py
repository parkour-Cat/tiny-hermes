"""What happens to a Feishu delivery between the wire and a claim.

Deliberately not a route: §2's whole point is that both transports converge
before anything downstream sees them, so the logic that decides *what an
inbound delivery means* lives where the WebSocket adapter can call it too.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from tiny_hermes.channels.domain.events import ChannelEvent, MalformedChannelEvent
from tiny_hermes.channels.domain.feishu import event_from_envelope
from tiny_hermes.channels.domain.webhook import (
    WebhookRefused,
    decrypt_payload,
    verify_signature,
)


@dataclass(frozen=True)
class BindingSecrets:
    binding_id: UUID
    encrypt_key: str


@dataclass(frozen=True)
class Challenge:
    """Feishu's registration handshake. Answering it is not optional: a
    webhook that cannot complete this cannot be configured at all, so an
    endpoint that only handled events would be one nobody could turn on."""

    challenge: str


def _unsigned_handshake(body: bytes, encrypt_key: str) -> Challenge:
    """The one thing an unsigned body is allowed to be.

    Unsigned does not mean plaintext. A real tenant sends the registration
    handshake with **no signature headers and an encrypted body** — the two
    are separate choices, and the first version of this function assumed
    they moved together. It read `{"encrypt": ...}` as the envelope, found
    no `type`, and refused a correct request as "only a handshake may arrive
    unsigned", which turned away exactly what the exception was written to
    let in.

    Decrypting here is safe in a way decrypting an unsigned *event* would
    not be: whatever comes out, only a `Challenge` can leave this function,
    so an attacker who guesses the URL still cannot get a body treated as an
    event without a signature.
    """
    try:
        parsed: object = json.loads(body)
    except json.JSONDecodeError as error:
        raise WebhookRefused("unsigned body is not JSON") from error
    if not isinstance(parsed, dict):
        raise WebhookRefused("unsigned body is not an object")
    envelope = cast(dict[str, Any], parsed)
    if "encrypt" in envelope:
        envelope = decrypt_payload(encrypt_key=encrypt_key, body=body)
    if envelope.get("type") != "url_verification":
        raise WebhookRefused("only a handshake may arrive unsigned")
    challenge = envelope.get("challenge")
    if not isinstance(challenge, str):
        raise WebhookRefused("url_verification carried no challenge")
    return Challenge(challenge=challenge)


@dataclass(frozen=True)
class Claimed:
    event: ChannelEvent
    #: `None` when this delivery was a duplicate. Not an error — Feishu
    #: delivers at-least-once, so duplicates are ordinary traffic and the
    #: endpoint still has to answer 200 or they will be retried forever.
    claim_id: UUID | None


class DeliveryClaims(Protocol):
    """Only the one question this service asks of storage. Narrow on
    purpose: the WebSocket adapter will call the same service, and a wide
    port would invite it to reach past this seam into the database the
    plan says it must not share."""

    async def claim_delivery(
        self, binding_id: UUID, channel_event_id: str, now: datetime
    ) -> UUID | None: ...


class FeishuWebhookService:
    def __init__(self, store: DeliveryClaims) -> None:
        self._store = store

    async def accept(
        self,
        *,
        secrets: BindingSecrets,
        body: bytes,
        timestamp: str | None,
        nonce: str | None,
        signature: str | None,
    ) -> Challenge | Claimed:
        """Verify, decrypt, normalize, claim — in that order, and no other.

        Verification comes first because everything after it treats the
        bytes as trustworthy: decrypting an unverified body would run this
        platform's cipher over an attacker's input, and normalizing one
        would let them choose an `event_id` and suppress a real delivery by
        claiming it first.
        """
        if timestamp is None or nonce is None or signature is None:
            # Feishu sends the *registration* handshake unsigned and in
            # plaintext, even with an Encrypt Key configured — the key only
            # governs the deliveries that follow. Requiring a signature here
            # made the endpoint impossible to register at all: the console
            # answered `Challenge code没有返回` and this platform logged a
            # refusal for a request that was exactly what the protocol says
            # to send.
            #
            # So one narrow door, and it opens onto nothing else. An unsigned
            # body may only be a `url_verification`; anything else is refused
            # here, before it is read as an event. Accepting an unsigned
            # event would let whoever learned this URL speak as any user of
            # the tenant, which is the whole reason a signature exists.
            return _unsigned_handshake(body, secrets.encrypt_key)

        verify_signature(
            timestamp=timestamp,
            nonce=nonce,
            encrypt_key=secrets.encrypt_key,
            body=body,
            signature=signature,
        )
        envelope = decrypt_payload(encrypt_key=secrets.encrypt_key, body=body)

        if envelope.get("type") == "url_verification":
            challenge = envelope.get("challenge")
            if not isinstance(challenge, str):
                raise WebhookRefused("url_verification carried no challenge")
            return Challenge(challenge=challenge)

        try:
            event = event_from_envelope(envelope)
        except MalformedChannelEvent as error:
            # A 400 rather than a refusal: the sender proved it was Feishu,
            # so this is a payload this platform does not understand, not an
            # intruder. Feishu retries 4xx, which is right — a message type
            # added here later should start working without a redeploy on
            # their side.
            raise error

        claim_id = await self._store.claim_delivery(
            secrets.binding_id, event.channel_event_id, datetime.now(UTC)
        )
        return Claimed(event=event, claim_id=claim_id)
