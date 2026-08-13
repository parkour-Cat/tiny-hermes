from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, status
from pydantic import BaseModel, Field
from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.domain.models import AuthenticatedUser
from tiny_hermes.identity.presentation.dependencies import (
    SESSION_COOKIE,
    authenticate_browser_user,
    forbidden,
    require_workspace_id,
    verify_browser_write,
)
from tiny_hermes.secrets.application.service import (
    ForbiddenSecretAction,
    InvalidSecretName,
    InvalidSecretPlaintext,
    KekMissing,
    PreviousKekMissing,
    SecretNameTaken,
    SecretService,
    UnknownSecret,
)
from tiny_hermes.secrets.domain.models import SecretScope, SecretView
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class CreateSecretRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scope: Literal["workspace", "platform"]
    plaintext: str = Field(min_length=1, max_length=65_536)


class SecretResponse(BaseModel):
    id: UUID
    name: str
    scope: str
    workspace_id: UUID | None
    status: str
    mask: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, secret: SecretView) -> "SecretResponse":
        return cls(
            id=secret.id,
            name=secret.name,
            scope=secret.scope.value,
            workspace_id=secret.workspace_id,
            status=secret.status.value,
            mask=secret.mask,
            created_at=secret.created_at,
            updated_at=secret.updated_at,
        )


class RewrapResponse(BaseModel):
    processed: int
    remaining: int
    current_key_id: str


def secret_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/secrets", tags=["secrets"])
    auth_dependency = resources.auth_service
    secrets_dependency = resources.secret_service

    @router.post("", response_model=SecretResponse, status_code=status.HTTP_201_CREATED)
    async def create_secret(  # pyright: ignore[reportUnusedFunction]
        payload: CreateSecretRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        secrets: Annotated[SecretService, Depends(secrets_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> SecretResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            created = await secrets.create(
                _actor(user),
                workspace_id,
                payload.name,
                SecretScope(payload.scope),
                payload.plaintext,
                request.state.request_id,
            )
        except ForbiddenSecretAction as error:
            raise forbidden() from error
        except KekMissing as error:
            raise _kek_missing() from error
        except InvalidSecretName as error:
            raise AppError(
                code="invalid_secret_name",
                title="Invalid secret name",
                status=422,
                detail="The secret name is invalid.",
            ) from error
        except InvalidSecretPlaintext as error:
            raise AppError(
                code="invalid_secret_plaintext",
                title="Invalid secret value",
                status=422,
                detail="A secret value is required.",
            ) from error
        except SecretNameTaken as error:
            raise AppError(
                code="secret_name_taken",
                title="Secret name taken",
                status=409,
                detail="That name already belongs to a secret in this scope.",
            ) from error
        return SecretResponse.from_domain(created)

    @router.get("", response_model=list[SecretResponse])
    async def list_secrets(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        secrets: Annotated[SecretService, Depends(secrets_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[SecretResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            listed = await secrets.list(
                _actor(user), workspace_id, request.state.request_id
            )
        except ForbiddenSecretAction as error:
            raise forbidden() from error
        return [SecretResponse.from_domain(item) for item in listed]

    @router.post("/rewrap", response_model=RewrapResponse)
    async def rewrap_secrets(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        secrets: Annotated[SecretService, Depends(secrets_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> RewrapResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            result = await secrets.rewrap(
                _actor(user), workspace_id, request.state.request_id
            )
        except ForbiddenSecretAction as error:
            raise forbidden() from error
        except KekMissing as error:
            raise _kek_missing() from error
        except PreviousKekMissing as error:
            raise AppError(
                code="previous_kek_missing",
                title="Previous KEK missing",
                status=503,
                detail="TINY_HERMES_PREVIOUS_KEK is missing or invalid.",
            ) from error
        return RewrapResponse(
            processed=result.processed,
            remaining=result.remaining,
            current_key_id=result.current_key_id,
        )

    @router.post("/{secret_id}/disable", response_model=SecretResponse)
    async def disable_secret(  # pyright: ignore[reportUnusedFunction]
        secret_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        secrets: Annotated[SecretService, Depends(secrets_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> SecretResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            disabled = await secrets.disable(
                _actor(user), workspace_id, secret_id, request.state.request_id
            )
        except ForbiddenSecretAction as error:
            raise forbidden() from error
        except UnknownSecret as error:
            raise AppError(
                code="secret_not_found",
                title="Secret not found",
                status=404,
                detail="No secret by that identifier is available in this workspace.",
            ) from error
        return SecretResponse.from_domain(disabled)

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _kek_missing() -> AppError:
    return AppError(
        code="kek_missing",
        title="KEK missing",
        status=503,
        detail="TINY_HERMES_KEK is missing or invalid, so a secret cannot be stored.",
    )
