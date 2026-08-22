"""The door Feishu knocks on.

Public by construction: Feishu holds no credential of this platform's, so
the signature over the body is the only thing separating this endpoint from
the internet. That is why `channel_bindings` cannot hold a Feishu binding
without an `encrypt_key_ref` (migration 0037) — a binding with no key would
turn this route into an open one.

Not under `_CONSOLE_ONLY`: that guard refuses a request carrying an
end-user cookie, which has nothing to do with this caller. Feishu arrives
with no cookies at all.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.channels.application.feishu_service import (
    Challenge,
    FeishuChannelService,
    UnknownChannelBinding,
)
from tiny_hermes.channels.application.ingestion import ErasedSubjectRefused
from tiny_hermes.channels.domain.events import MalformedChannelEvent
from tiny_hermes.channels.domain.webhook import WebhookRefused
from tiny_hermes.shared.errors import AppError


class ChallengeResponse(BaseModel):
    """Feishu's registration handshake expects exactly this and nothing
    else. A webhook that cannot answer it cannot be saved, so this response
    is what makes the endpoint configurable at all."""

    challenge: str


class DeliveryAccepted(BaseModel):
    #: Present when this delivery started work. Absent for a duplicate and
    #: for the handshake — both of which are still 200, because anything
    #: else has Feishu retrying something that already succeeded.
    run_id: UUID | None = None
    #: §497: the pending Run was saved and this says why it is waiting. A
    #: transport rendering a card reads this; one that ignores it is the
    #: silence §19.2 forbids.
    blocked_by_run_id: UUID | None = None
    queue_position: int | None = None
    available_actions: list[str] = []


def _refused() -> AppError:
    """One refusal for every way a delivery can fail to prove itself.

    Signature mismatch, unknown binding, disabled binding, a binding of
    another channel: all 401 with the same body. Telling them apart would
    let anyone holding the URL map which bindings exist and which are
    switched off.
    """
    return AppError(
        code="channel_delivery_refused",
        title="Delivery refused",
        status=status.HTTP_401_UNAUTHORIZED,
        detail="This delivery could not be verified.",
    )


def feishu_webhook_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/channels/feishu", tags=["channels"])
    service_dependency = resources.feishu_channel_service

    @router.post("/{binding_id}/webhook")
    async def deliver(  # pyright: ignore[reportUnusedFunction]
        binding_id: UUID,
        request: Request,
        response: Response,
        service: Annotated[
            FeishuChannelService, Depends(service_dependency, scope="function")
        ],
        x_lark_request_timestamp: Annotated[str | None, Header()] = None,
        x_lark_request_nonce: Annotated[str | None, Header()] = None,
        x_lark_signature: Annotated[str | None, Header()] = None,
    ) -> ChallengeResponse | DeliveryAccepted:
        # The raw bytes, not a parsed model: the signature covers the body
        # exactly as sent, and re-serializing a parsed object would change
        # it. FastAPI would happily have given a typed body here and the
        # signature would then fail for well-formed requests.
        body = await request.body()
        if (
            x_lark_request_timestamp is None
            or x_lark_request_nonce is None
            or x_lark_signature is None
        ):
            raise _refused()

        try:
            outcome = await service.deliver(
                binding_id=binding_id,
                body=body,
                timestamp=x_lark_request_timestamp,
                nonce=x_lark_request_nonce,
                signature=x_lark_signature,
                request_id=request.state.request_id,
            )
        except (WebhookRefused, UnknownChannelBinding) as error:
            raise _refused() from error
        except ErasedSubjectRefused as error:
            # §344 holds across transports. Refused like any unverified
            # delivery rather than explained: confirming that this person
            # once existed and was erased is itself disclosure.
            raise _refused() from error
        except MalformedChannelEvent as error:
            # Verified, so this really is Feishu — just a message this
            # platform does not handle. Answered 200 rather than 4xx:
            # retries are finite and would not make an unsupported message
            # type supported, so refusing only produces four more copies of
            # something already understood.
            del error
            return DeliveryAccepted()

        if isinstance(outcome, Challenge):
            return ChallengeResponse(challenge=outcome.challenge)

        response.status_code = status.HTTP_200_OK
        if outcome.delivered is None:
            return DeliveryAccepted()

        blocked = outcome.delivered.blocked
        return DeliveryAccepted(
            run_id=outcome.delivered.run.run_id,
            blocked_by_run_id=None if blocked is None else blocked.blocked_by_run_id,
            queue_position=None if blocked is None else blocked.position,
            available_actions=[] if blocked is None else list(blocked.available_actions),
        )

    return router
