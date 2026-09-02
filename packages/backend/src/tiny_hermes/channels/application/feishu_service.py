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
from typing import Any, Protocol
from uuid import UUID

from tiny_hermes.channels.application.ingestion import (
    ChannelBindingRecord,
    ChannelIngestion,
    Delivered,
)
from tiny_hermes.channels.application.webhook_service import (
    BindingSecrets,
    Challenge,
    Claimed,
    FeishuWebhookService,
    Unreadable,
)
from tiny_hermes.channels.domain.blocked import BlockedNotice
from tiny_hermes.channels.domain.command_receipt import CommandReceipt
from tiny_hermes.channels.domain.feishu import CHANNEL

logger = logging.getLogger(__name__)


class BindingDirectory(Protocol):
    """What this service asks of storage. Narrow so the endpoint cannot
    reach past it into anything else the store can do."""

    async def active_binding(self, binding_id: UUID) -> ChannelBindingRecord | None: ...

    async def encrypt_key_ref_of(self, binding_id: UUID) -> str | None: ...

    async def attach_run(self, event_row_id: UUID, run_id: UUID) -> None: ...

    async def record_blocked_notice(
        self, event_row_id: UUID, notice: BlockedNotice
    ) -> None: ...

    async def record_unsupported(
        self, event_row_id: UUID, kind: str, external_user_id: str
    ) -> None: ...

    async def record_command_receipt(
        self, event_row_id: UUID, receipt: CommandReceipt, external_user_id: str
    ) -> None: ...


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

        return await self._after_claim(binding, outcome, request_id)

    async def deliver_verified(
        self,
        *,
        binding_id: UUID,
        envelope: dict[str, Any],
        request_id: str,
    ) -> Accepted:
        """A delivery somebody else already authenticated, from claim to Run.

        For the long connection, where Feishu's SDK has verified and
        decrypted the frame before this platform sees it — so there is no
        body, no signature and no `encrypt_key` here, and no `Challenge`
        either: the handshake belongs to the HTTP endpoint a long-connection
        binding does not have.

        **The caller must have established that the envelope is authentic**,
        the same requirement `accept_verified` carries and for the same
        reason: nothing below this line can check one, and a caller that
        skipped it would let an attacker choose the `event_id` and suppress
        a real delivery by claiming it first.

        The binding is still looked up, and still through `_binding`, so a
        binding disabled while the socket stayed open is refused here rather
        than starting a Run for a channel the workspace has switched off.
        """
        binding = await self._binding(binding_id)
        outcome = await self._webhooks.accept_verified(
            binding_id=binding_id, envelope=envelope
        )
        return await self._after_claim(binding, outcome, request_id)

    async def _after_claim(
        self, binding: ChannelBindingRecord, outcome: Claimed | Unreadable, request_id: str
    ) -> Accepted:
        """Everything a claimed delivery still owes, for either transport.

        Here rather than duplicated per transport because every branch below
        is a way this platform has already failed to answer somebody, and a
        second copy would be a second place to forget one — the long
        connection stopped at the claim for a whole task and produced
        exactly that: rows written, nobody reached.
        """
        if isinstance(outcome, Unreadable):
            # No Run — there is nothing this build could hand an Agent. But
            # there is a person waiting, so the delivery is marked for a
            # refusal the scan will send. `claim_id` is None for a retry of
            # a photo already answered, and then nothing is marked: one
            # message, one refusal.
            if outcome.claim_id is not None:
                await self._bindings.record_unsupported(
                    outcome.claim_id, outcome.kind, outcome.external_user_id
                )
            return Accepted(delivered=None)

        if outcome.claim_id is None:
            # The claim went to another delivery of the same event. Doing
            # nothing here is the deduplication working, and answering 200
            # is what stops Feishu retrying it for six hours.
            return Accepted(delivered=None)

        delivered = await self._ingestion.run_for(
            binding=binding, event=outcome.event, request_id=request_id
        )
        if delivered.run is None:
            # A command took the claim instead of becoming a Run: there is
            # no `run_id` to attach and nothing that can be blocked (a
            # command that never queued cannot be queued behind). What it
            # does owe is the same thing `record_unsupported` owes above —
            # a sender who is not told anything reads silence as a lost
            # message. Recorded here rather than inside `run_for` because
            # `event_row_id` (`outcome.claim_id`) is `ChannelIngestion`'s
            # caller's to know, not its own — the same reason `attach_run`
            # below lives here and not there.
            if delivered.receipt is not None:
                await self._bindings.record_command_receipt(
                    outcome.claim_id, delivered.receipt, outcome.event.external_user_id
                )
            return Accepted(delivered=delivered)
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
        if delivered.blocked is not None:
            # §19.2: a blocked Session may not swallow the message quietly.
            # Recorded here rather than sent here — sending inside this
            # transaction would make an inbound delivery depend on
            # `open.feishu.cn` being reachable, and a timeout would leave
            # Feishu retrying a delivery whose claim is already taken.
            await self._bindings.record_blocked_notice(
                outcome.claim_id, delivered.blocked
            )
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
