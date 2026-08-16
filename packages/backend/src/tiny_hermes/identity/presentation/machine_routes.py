from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, status
from pydantic import BaseModel, Field

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.application.machine_service import (
    ForbiddenMachineAction,
    InvalidApiKeyScopes,
    InvalidServiceAccountName,
    InvalidServiceAccountRole,
    MachineIdentityService,
    ServiceAccountNameTaken,
    UnknownApiKey,
    UnknownServiceAccount,
)
from tiny_hermes.identity.domain.models import ApiKey, AuthenticatedUser, ServiceAccount
from tiny_hermes.identity.presentation.dependencies import (
    SESSION_COOKIE,
    authenticate_browser_user,
    forbidden,
    require_workspace_id,
    verify_browser_write,
)
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor, Role

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class CreateServiceAccountRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: Literal["developer", "viewer"]


class CreateApiKeyRequest(BaseModel):
    scopes: list[str] = Field(min_length=1)
    agent_ids: list[UUID] = Field(default_factory=lambda: list[UUID]())
    expires_at: datetime | None = None


class ServiceAccountResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    role: str
    status: str
    created_by_user_id: UUID
    created_at: datetime

    @classmethod
    def from_domain(cls, account: ServiceAccount) -> "ServiceAccountResponse":
        return cls(
            id=account.id,
            workspace_id=account.workspace_id,
            name=account.name,
            role=account.role.value,
            status=account.status.value,
            created_by_user_id=account.created_by_user_id,
            created_at=account.created_at,
        )


class ApiKeyResponse(BaseModel):
    id: UUID
    service_account_id: UUID
    prefix: str
    scopes: list[str]
    agent_ids: list[UUID]
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime

    @classmethod
    def from_domain(cls, key: ApiKey) -> "ApiKeyResponse":
        return cls(
            id=key.id,
            service_account_id=key.service_account_id,
            prefix=key.prefix,
            scopes=list(key.scopes),
            agent_ids=list(key.agent_ids),
            expires_at=key.expires_at,
            revoked_at=key.revoked_at,
            created_at=key.created_at,
        )


class IssuedApiKeyResponse(ApiKeyResponse):
    token: str


def machine_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["machine-identity"])
    auth_dependency = resources.auth_service
    machines_dependency = resources.machine_identity_service

    @router.post(
        "/service-accounts",
        response_model=ServiceAccountResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_service_account(  # pyright: ignore[reportUnusedFunction]
        payload: CreateServiceAccountRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> ServiceAccountResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            account = await machines.create_account(
                _actor(user),
                workspace_id,
                payload.name,
                Role(payload.role),
                request.state.request_id,
            )
        except ForbiddenMachineAction as error:
            raise forbidden() from error
        except InvalidServiceAccountRole as error:
            raise AppError(
                code="invalid_service_account_role",
                title="Invalid service account role",
                status=422,
                detail="A service account may be a developer or a viewer, never an admin.",
            ) from error
        except InvalidServiceAccountName as error:
            raise AppError(
                code="invalid_service_account_name",
                title="Invalid service account name",
                status=422,
                detail="The service account name is invalid.",
            ) from error
        except ServiceAccountNameTaken as error:
            raise AppError(
                code="service_account_name_taken",
                title="Service account name taken",
                status=409,
                detail="That name already belongs to a service account in this workspace.",
            ) from error
        return ServiceAccountResponse.from_domain(account)

    @router.get("/service-accounts", response_model=list[ServiceAccountResponse])
    async def list_service_accounts(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[ServiceAccountResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            accounts = await machines.list_accounts(
                _actor(user), workspace_id, request.state.request_id
            )
        except ForbiddenMachineAction as error:
            raise forbidden() from error
        return [ServiceAccountResponse.from_domain(item) for item in accounts]

    @router.post(
        "/service-accounts/{account_id}/disable",
        response_model=ServiceAccountResponse,
    )
    async def disable_service_account(  # pyright: ignore[reportUnusedFunction]
        account_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> ServiceAccountResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            account = await machines.disable_account(
                _actor(user), workspace_id, account_id, request.state.request_id
            )
        except ForbiddenMachineAction as error:
            raise forbidden() from error
        except UnknownServiceAccount as error:
            raise _unknown_account() from error
        return ServiceAccountResponse.from_domain(account)

    @router.post(
        "/service-accounts/{account_id}/api-keys",
        response_model=IssuedApiKeyResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_api_key(  # pyright: ignore[reportUnusedFunction]
        account_id: UUID,
        payload: CreateApiKeyRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> IssuedApiKeyResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            issued = await machines.create_key(
                _actor(user),
                workspace_id,
                account_id,
                tuple(payload.scopes),
                request.state.request_id,
                tuple(payload.agent_ids),
                payload.expires_at,
            )
        except ForbiddenMachineAction as error:
            raise forbidden() from error
        except UnknownServiceAccount as error:
            raise _unknown_account() from error
        except InvalidApiKeyScopes as error:
            raise AppError(
                code="invalid_api_key_scopes",
                title="Invalid API key scopes",
                status=422,
                detail="Scopes must be a subset of the closed set the account's role permits.",
            ) from error
        body = ApiKeyResponse.from_domain(issued.key)
        return IssuedApiKeyResponse(**body.model_dump(), token=issued.token)

    @router.get(
        "/service-accounts/{account_id}/api-keys",
        response_model=list[ApiKeyResponse],
    )
    async def list_api_keys(  # pyright: ignore[reportUnusedFunction]
        account_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[ApiKeyResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            keys = await machines.list_keys(
                _actor(user), workspace_id, account_id, request.state.request_id
            )
        except ForbiddenMachineAction as error:
            raise forbidden() from error
        except UnknownServiceAccount as error:
            raise _unknown_account() from error
        return [ApiKeyResponse.from_domain(item) for item in keys]

    @router.post("/api-keys/{key_id}/revoke", response_model=ApiKeyResponse)
    async def revoke_api_key(  # pyright: ignore[reportUnusedFunction]
        key_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> ApiKeyResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            key = await machines.revoke_key(
                _actor(user), workspace_id, key_id, request.state.request_id
            )
        except ForbiddenMachineAction as error:
            raise forbidden() from error
        except UnknownApiKey as error:
            raise AppError(
                code="api_key_not_found",
                title="API key not found",
                status=404,
                detail="No API key by that identifier is available in this workspace.",
            ) from error
        return ApiKeyResponse.from_domain(key)

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _unknown_account() -> AppError:
    return AppError(
        code="service_account_not_found",
        title="Service account not found",
        status=404,
        detail="No service account by that identifier is available in this workspace.",
    )
