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

import logging
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
from pydantic import BaseModel, Field

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.channels.application.binding_service import (
    ChannelAlreadyBound,
    ChannelBindingService,
    ChannelBindingView,
    ChannelKeyRequired,
    ChannelKeyUnknown,
    ForbiddenChannelAction,
    UnknownChannel,
)
from tiny_hermes.channels.application.feishu_service import (
    Challenge,
    FeishuChannelService,
    UnknownChannelBinding,
)
from tiny_hermes.channels.application.ingestion import ErasedSubjectRefused
from tiny_hermes.channels.domain.events import MalformedChannelEvent
from tiny_hermes.channels.domain.webhook import WebhookRefused
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.domain.models import AuthenticatedUser
from tiny_hermes.identity.presentation.dependencies import (
    SESSION_COOKIE,
    authenticate_browser_user,
    forbidden,
    require_workspace_id,
    verify_browser_write,
)
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

logger = logging.getLogger(__name__)

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


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
        # Not refused here any more. Feishu sends the registration handshake
        # unsigned, so this route cannot tell "unsigned" from "unacceptable"
        # on its own; `webhook_service` decides, and it lets exactly one kind
        # of unsigned body through — a `url_verification`. Everything else
        # still needs a signature that verifies.
        if x_lark_signature is None:
            logger.info("feishu delivery arrived unsigned: binding=%s", binding_id)

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
            # The caller's answer stays uniform (see `_refused`), but an
            # operator watching their own deployment needs the reason:
            # "signature does not match" points at an Encrypt Key differing
            # from the one this binding names, which is a different fix from
            # an unknown binding or a malformed body.
            logger.warning(
                "feishu delivery refused: binding=%s reason=%r ts=%s nonce=%s "
                "sig=%s body=%s",
                binding_id,
                error,
                x_lark_request_timestamp,
                x_lark_request_nonce,
                x_lark_signature,
                body[:200],
            )
            raise _refused() from error
        except ErasedSubjectRefused as error:
            # §344 holds across transports. Refused like any unverified
            # delivery rather than explained: confirming that this person
            # once existed and was erased is itself disclosure.
            raise _refused() from error
        except MalformedChannelEvent as error:
            # An envelope with no sender or no event id — genuinely broken,
            # and the only case left here. A message type this build cannot
            # read no longer reaches this branch: `webhook_service` claims
            # it and marks it for a refusal the scan sends, because that one
            # has somebody to answer.
            #
            # This one does not. **Silent to any person, and it has to be**:
            # there is no sender named in the envelope, so a reply would go
            # to whoever the platform guessed. An earlier version of this
            # comment claimed `never silently` on the strength of the log
            # line below — which is read by operators and not by anybody
            # waiting for an answer.
            #
            # Answered 200 rather than 4xx: retries are finite and would not
            # make a broken envelope readable, so refusing only produces
            # four more copies of something already understood.
            logger.warning(
                "feishu delivery could not be read and names nobody to tell: "
                "binding=%s reason=%r",
                binding_id,
                error,
            )
            return DeliveryAccepted()

        if isinstance(outcome, Challenge):
            return ChallengeResponse(challenge=outcome.challenge)

        response.status_code = status.HTTP_200_OK
        if outcome.delivered is None:
            return DeliveryAccepted()

        blocked = outcome.delivered.blocked
        run = outcome.delivered.run
        # `run_id` was already optional for a duplicate and the handshake
        # (see `DeliveryAccepted.run_id`'s own docstring); a command is a
        # third reason it comes back `None` here. Its own reply does not
        # travel through this response at all — it reaches the sender
        # later, through the outbound scan that reads
        # `pending_command_receipts`.
        return DeliveryAccepted(
            run_id=None if run is None else run.run_id,
            blocked_by_run_id=None if blocked is None else blocked.blocked_by_run_id,
            queue_position=None if blocked is None else blocked.position,
            available_actions=[] if blocked is None else list(blocked.available_actions),
        )

    return router


class CreateChannelBindingRequest(BaseModel):
    channel: Literal["feishu"]
    agent_id: UUID
    #: The tenant's own identifier for the app. Metadata: it names which app
    #: this binding belongs to, and is not a credential.
    app_id: str | None = Field(default=None, max_length=120)
    #: The **name of a workspace secret**, never a key. §4.6 lets an
    #: administrator manage this metadata without ever seeing plaintext, and
    #: a field that took the key itself would make that impossible to keep.
    encrypt_key_ref: str | None = Field(default=None, max_length=200)
    #: The name of the workspace secret holding the app secret, used to
    #: reply. Optional: a binding with none is receive-only.
    app_secret_ref: str | None = Field(default=None, max_length=200)


class UpdateChannelBindingRequest(BaseModel):
    """Credentials and `app_id`, and deliberately nothing else.

    A field left out is unchanged; a field sent as `null` is cleared. The
    route reads `model_fields_set` to tell them apart, which is the whole
    reason this model exists rather than reusing the create request.

    `agent_id` and `channel` are absent on purpose: moving a binding to
    another Agent would silently redirect every conversation already mapped
    in `channel_conversations`, and that is not a credential fix.
    """

    app_id: str | None = Field(default=None, max_length=120)
    encrypt_key_ref: str | None = Field(default=None, max_length=200)
    app_secret_ref: str | None = Field(default=None, max_length=200)


class ChannelBindingResponse(BaseModel):
    id: UUID
    channel: str
    agent_id: UUID
    status: str
    app_id: str | None
    encrypt_key_ref: str | None
    app_secret_ref: str | None
    created_by: UUID
    created_at: datetime

    @classmethod
    def of(cls, view: ChannelBindingView) -> "ChannelBindingResponse":
        return cls(
            id=view.id,
            channel=view.channel,
            agent_id=view.agent_id,
            status=view.status,
            app_id=view.app_id,
            encrypt_key_ref=view.encrypt_key_ref,
            app_secret_ref=view.app_secret_ref,
            created_by=view.created_by,
            created_at=view.created_at,
        )


def _binding_error(error: Exception) -> AppError:
    if isinstance(error, ForbiddenChannelAction):
        return forbidden()
    if isinstance(error, ChannelKeyRequired):
        return AppError(
            code="channel_key_required",
            title="A key reference is required",
            status=400,
            detail=(
                "This channel's deliveries are encrypted, so the binding must "
                "name the workspace secret holding its encrypt key."
            ),
        )
    if isinstance(error, ChannelKeyUnknown):
        return AppError(
            code="channel_key_unknown",
            title="No such secret",
            status=400,
            detail=(
                "No active workspace secret has that name. A binding pointing "
                "at a secret that does not exist accepts deliveries it can "
                "never decrypt."
            ),
        )
    if isinstance(error, ChannelAlreadyBound):
        return AppError(
            code="channel_already_bound",
            title="Already bound",
            status=409,
            detail="This Agent is already bound to that channel.",
        )
    return AppError(
        code="channel_binding_not_found",
        title="Channel binding not found",
        status=404,
        detail="No such channel binding exists in the selected workspace.",
    )


def channel_binding_router(resources: ApplicationResources) -> APIRouter:
    """§20.1's Channels, which had no rows anyone could make.

    Every route here is workspace-scoped through `X-Workspace-Id`; none of
    them is the delivery path, which is unauthenticated by necessity and
    lives in `feishu_webhook_router` above.
    """
    router = APIRouter(prefix="/api/v1/channel-bindings", tags=["channels"])
    auth_dependency = resources.auth_service
    service_dependency = resources.channel_binding_service

    @router.post("", response_model=ChannelBindingResponse, status_code=201)
    async def create_binding(  # pyright: ignore[reportUnusedFunction]
        payload: CreateChannelBindingRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            ChannelBindingService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> ChannelBindingResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            created = await service.create(
                _actor(user),
                workspace_id,
                channel=payload.channel,
                agent_id=payload.agent_id,
                app_id=payload.app_id,
                encrypt_key_ref=payload.encrypt_key_ref,
                app_secret_ref=payload.app_secret_ref,
                request_id=request.state.request_id,
            )
        except (
            ForbiddenChannelAction,
            ChannelKeyRequired,
            ChannelKeyUnknown,
            ChannelAlreadyBound,
            UnknownChannel,
        ) as error:
            raise _binding_error(error) from error
        return ChannelBindingResponse.of(created)

    @router.get("", response_model=list[ChannelBindingResponse])
    async def list_bindings(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            ChannelBindingService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[ChannelBindingResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            listed = await service.list(
                _actor(user), workspace_id, request.state.request_id
            )
        except ForbiddenChannelAction as error:
            raise _binding_error(error) from error
        return [ChannelBindingResponse.of(item) for item in listed]

    @router.patch("/{binding_id}", response_model=ChannelBindingResponse)
    async def update_binding(  # pyright: ignore[reportUnusedFunction]
        binding_id: UUID,
        payload: UpdateChannelBindingRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            ChannelBindingService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> ChannelBindingResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            updated = await service.update(
                _actor(user),
                workspace_id,
                binding_id,
                # `model_fields_set` rather than the values: a field absent
                # from the body and one sent as `null` are different
                # requests, and Pydantic's default would render both as
                # `None`. Without this an update that fixed the app secret
                # would strip the encrypt key and break inbound.
                changes={
                    name: getattr(payload, name)
                    for name in payload.model_fields_set
                },
                request_id=request.state.request_id,
            )
        except (
            ForbiddenChannelAction,
            ChannelKeyRequired,
            ChannelKeyUnknown,
            UnknownChannel,
        ) as error:
            raise _binding_error(error) from error
        return ChannelBindingResponse.of(updated)

    @router.post("/{binding_id}/disable", response_model=ChannelBindingResponse)
    async def disable_binding(  # pyright: ignore[reportUnusedFunction]
        binding_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        service: Annotated[
            ChannelBindingService, Depends(service_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> ChannelBindingResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            updated = await service.disable(
                _actor(user), workspace_id, binding_id, request.state.request_id
            )
        except (ForbiddenChannelAction, UnknownChannel) as error:
            raise _binding_error(error) from error
        return ChannelBindingResponse.of(updated)

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(id=user.id, is_platform_admin=user.is_platform_admin)
