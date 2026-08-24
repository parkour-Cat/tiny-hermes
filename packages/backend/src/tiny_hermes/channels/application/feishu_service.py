"""The Feishu Webhook endpoint's own composition root.

Everything below it was already testable without a tenant. This is the
piece that makes it *reachable*: a binding is looked up, its key resolved,
the delivery verified and claimed, and a Run started as the person who sent
it. Without this the modules underneath were code nobody could call from
the network — which is the shape of failure this project has produced five
times already and named in its own verification records.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from tiny_hermes.channels.application.ingestion import (
    ChannelBindingRecord,
    ChannelIngestion,
    Delivered,
)
from tiny_hermes.channels.application.webhook_service import (
    BindingSecrets,
    Challenge,
    FeishuWebhookService,
)
from tiny_hermes.channels.domain.feishu import CHANNEL

logger = logging.getLogger(__name__)


class BindingDirectory(Protocol):
    """What this service asks of storage. Narrow so the endpoint cannot
    reach past it into anything else the store can do."""

    async def active_binding(self, binding_id: UUID) -> ChannelBindingRecord | None: ...

    async def encrypt_key_ref_of(self, binding_id: UUID) -> str | None: ...

    async def attach_run(self, event_row_id: UUID, run_id: UUID) -> None: ...


class UnknownChannelBinding(Exception):
    """Unknown, disabled, or not a Feishu binding — one exception for all
    three. Distinguishing them would let anyone holding the URL enumerate
    which bindings exist and which are switched off."""


@dataclass(frozen=True)
class Accepted:
    """A delivery that was verified and claimed. `delivered` is `None` when
    the claim went to somebody else — a duplicate, which is ordinary traffic
    under at-least-once delivery and must still answer 200."""

    delivered: Delivered | None


class FeishuChannelService:
    def __init__(
        self,
        *,
        bindings: BindingDirectory,
        resolve_secret: Callable[[str], Awaitable[str]],
        webhooks: FeishuWebhookService,
        ingestion: ChannelIngestion,
    ) -> None:
        self._bindings = bindings
        self._resolve_secret = resolve_secret
        self._webhooks = webhooks
        self._ingestion = ingestion

    async def deliver(
        self,
        *,
        binding_id: UUID,
        body: bytes,
        #: `None` when Feishu sent no signature. Only a registration
        #: handshake may arrive that way — `webhook_service` enforces it.
        timestamp: str | None,
        nonce: str | None,
        signature: str | None,
        request_id: str,
    ) -> Challenge | Accepted:
        binding = await self._binding(binding_id)
        key = await self._key_for(binding_id)

        # Logged before the attempt so a refusal below has context: which
        # binding, and which secret its key came from. Never the key.
        logger.info(
            "feishu delivery: binding=%s key_ref=%s bytes=%d",
            binding_id,
            await self._bindings.encrypt_key_ref_of(binding_id),
            len(body),
        )
        outcome = await self._webhooks.accept(
            secrets=BindingSecrets(binding_id=binding_id, encrypt_key=key),
            body=body,
            timestamp=timestamp,
            nonce=nonce,
            signature=signature,
        )
        if isinstance(outcome, Challenge):
            return outcome

        if outcome.claim_id is None:
            # The claim went to another delivery of the same event. Doing
            # nothing here is the deduplication working, and answering 200
            # is what stops Feishu retrying it for six hours.
            return Accepted(delivered=None)

        delivered = await self._ingestion.run_for(
            binding=binding, event=outcome.event, request_id=request_id
        )
        # The claim and the Run, joined. This call was missing for a whole
        # milestone: a live deployment held two claims with `run_id` NULL
        # beside two completed Runs, and nothing failed, because nothing
        # read the column. It is the outbound queue's key now — a delivery
        # with no Run attached is a person who never gets an answer — so
        # losing this line stops replies instead of quietly losing a link.
        #
        # Same session as the claim (see `resources.feishu_channel_service`),
        # so the Run and its attachment commit together.
        await self._bindings.attach_run(outcome.claim_id, delivered.run.run_id)
        return Accepted(delivered=delivered)

    async def _binding(self, binding_id: UUID) -> ChannelBindingRecord:
        binding = await self._bindings.active_binding(binding_id)
        if binding is None or binding.channel != CHANNEL:
            raise UnknownChannelBinding
        return binding

    async def _key_for(self, binding_id: UUID) -> str:
        ref = await self._bindings.encrypt_key_ref_of(binding_id)
        if ref is None:
            # Migration 0037's CHECK makes this unreachable for a Feishu
            # binding. Refused rather than trusted anyway: if it ever became
            # reachable, the alternative is accepting unsigned deliveries.
            raise UnknownChannelBinding
        return await self._resolve_secret(ref)
