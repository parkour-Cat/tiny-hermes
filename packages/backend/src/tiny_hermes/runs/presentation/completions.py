from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.machine_service import MachineIdentityService
from tiny_hermes.identity.presentation.dependencies import resolve_api_key_caller
from tiny_hermes.runs.application.completions import (
    CompletionsError,
    admit_chat,
    complete_chat,
    stream_admitted,
)

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]
SessionHeader = Annotated[str | None, Header(alias="X-Tiny-Hermes-Session-Id")]
AuthorizationHeader = Annotated[str | None, Header()]


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str | list[Any] | None = None


class ChatCompletionsRequest(BaseModel):
    """OpenAI's extra fields are ignored; they are not session keys."""

    model_config = ConfigDict(extra="ignore")

    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    user: str | None = None


def completions_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(tags=["completions"])
    machines_dependency = resources.machine_identity_service

    @router.post("/v1/chat/completions", response_model=None)
    async def create_completion(  # pyright: ignore[reportUnusedFunction]
        payload: ChatCompletionsRequest,
        request: Request,
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        idempotency_key: IdempotencyHeader = None,
        session_header: SessionHeader = None,
        authorization: AuthorizationHeader = None,
    ) -> dict[str, Any] | JSONResponse | StreamingResponse:
        caller = await resolve_api_key_caller(
            machines,
            authorization=authorization,
            workspace_header=selected_workspace,
            required_scope="runs.write",
        )
        try:
            if payload.stream:
                admitted = await admit_chat(
                    sessions=resources.session_factory(),
                    actor=caller.actor,
                    workspace_id=caller.workspace_id,
                    model=payload.model,
                    messages=[
                        message.model_dump(mode="json") for message in payload.messages
                    ],
                    stream=True,
                    idempotency_key=idempotency_key,
                    session_header=session_header,
                    request_id=request.state.request_id,
                )
                return StreamingResponse(
                    stream_admitted(
                        admitted,
                        sessions=resources.session_factory(),
                        notifier=resources.wake_up_notifier(),
                        actor=caller.actor,
                        workspace_id=caller.workspace_id,
                    ),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache"},
                )
            return await complete_chat(
                sessions=resources.session_factory(),
                notifier=resources.wake_up_notifier(),
                actor=caller.actor,
                workspace_id=caller.workspace_id,
                model=payload.model,
                messages=[message.model_dump(mode="json") for message in payload.messages],
                stream=False,
                idempotency_key=idempotency_key,
                session_header=session_header,
                request_id=request.state.request_id,
            )
        except CompletionsError as error:
            return JSONResponse(status_code=error.status, content=error.payload())

    return router
