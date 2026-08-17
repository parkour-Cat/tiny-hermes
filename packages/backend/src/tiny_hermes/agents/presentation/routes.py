from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
from pydantic import BaseModel, Field

from tiny_hermes.agents.application.service import (
    AgentAliasAlreadyUsed,
    AgentCatalog,
    AgentCatalogError,
    DraftRevisionConflict,
    ForbiddenAgentAction,
    InvalidAgentAlias,
    InvalidAgentName,
    InvalidAgentSpec,
    ModelEndpointUnavailable,
    ModelOutputLimitTooHigh,
    RoundCeilingExceeded,
    UnknownAgent,
)
from tiny_hermes.agents.domain.models import Agent, AgentDraft, AgentVersion
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
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    alias: str = Field(min_length=1, max_length=80)


class ReplaceDraftRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    spec: dict[str, Any]


class PublishRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class RollbackRequest(BaseModel):
    version_id: UUID


class UpdateAgentRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    alias: str | None = Field(default=None, min_length=1, max_length=80)


class AgentResponse(BaseModel):
    id: UUID
    name: str
    alias: str
    status: str
    current_version_id: UUID | None
    created_at: datetime

    @classmethod
    def from_domain(cls, agent: Agent) -> "AgentResponse":
        return cls(
            id=agent.id,
            name=agent.name,
            alias=agent.alias,
            status=agent.status,
            current_version_id=agent.current_version_id,
            created_at=agent.created_at,
        )


class AgentDraftResponse(BaseModel):
    agent_id: UUID
    revision: int
    spec: dict[str, Any]
    updated_at: datetime

    @classmethod
    def from_domain(cls, draft: AgentDraft) -> "AgentDraftResponse":
        return cls(
            agent_id=draft.agent_id,
            revision=draft.revision,
            spec=draft.spec.model_dump(mode="json"),
            updated_at=draft.updated_at,
        )


class AgentVersionResponse(BaseModel):
    """Published version identity only; the spec is never echoed in errors."""

    id: UUID
    agent_id: UUID
    version_number: int
    schema_version: int
    content_hash: str
    created_at: datetime

    @classmethod
    def from_domain(cls, version: AgentVersion) -> "AgentVersionResponse":
        return cls(
            id=version.id,
            agent_id=version.agent_id,
            version_number=version.version_number,
            schema_version=version.schema_version,
            content_hash=version.content_hash,
            created_at=version.created_at,
        )


class AgentVersionDetailResponse(AgentVersionResponse):
    """One version, including the spec the Builder diffs against."""

    spec: dict[str, Any]

    @classmethod
    def from_domain(cls, version: AgentVersion) -> "AgentVersionDetailResponse":
        return cls(
            id=version.id,
            agent_id=version.agent_id,
            version_number=version.version_number,
            schema_version=version.schema_version,
            content_hash=version.content_hash,
            created_at=version.created_at,
            spec=version.spec,
        )


def agent_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/agents", tags=["agents"])
    auth_dependency = resources.auth_service
    catalog_dependency = resources.agent_catalog

    @router.get("", response_model=list[AgentResponse])
    async def list_agents(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[AgentCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[AgentResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            agents = await catalog.list_agents(
                workspace_id, _actor(user), request.state.request_id
            )
        except AgentCatalogError as error:
            raise _as_app_error(error) from error
        return [AgentResponse.from_domain(agent) for agent in agents]

    @router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
    async def create_agent(  # pyright: ignore[reportUnusedFunction]
        payload: CreateAgentRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[AgentCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> AgentResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            agent = await catalog.create_agent(
                workspace_id,
                _actor(user),
                payload.name,
                payload.alias,
                request.state.request_id,
            )
        except AgentCatalogError as error:
            raise _as_app_error(error) from error
        return AgentResponse.from_domain(agent)

    @router.get("/{agent_id}", response_model=AgentResponse)
    async def get_agent(  # pyright: ignore[reportUnusedFunction]
        agent_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[AgentCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> AgentResponse:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            agent = await catalog.get_agent(
                workspace_id, _actor(user), agent_id, request.state.request_id
            )
        except AgentCatalogError as error:
            raise _as_app_error(error) from error
        return AgentResponse.from_domain(agent)

    @router.patch("/{agent_id}", response_model=AgentResponse)
    async def update_agent(  # pyright: ignore[reportUnusedFunction]
        agent_id: UUID,
        payload: UpdateAgentRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[AgentCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> AgentResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        if payload.name is None and payload.alias is None:
            raise AppError(
                code="invalid_agent_update",
                title="Invalid agent update",
                status=422,
                detail="A rename must include a name, an alias, or both.",
            )
        try:
            agent = await catalog.update_agent(
                workspace_id,
                _actor(user),
                agent_id,
                payload.name,
                payload.alias,
                request.state.request_id,
            )
        except AgentCatalogError as error:
            raise _as_app_error(error) from error
        return AgentResponse.from_domain(agent)

    @router.get("/{agent_id}/draft", response_model=AgentDraftResponse)
    async def get_draft(  # pyright: ignore[reportUnusedFunction]
        agent_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[AgentCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> AgentDraftResponse:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            draft = await catalog.get_draft(
                workspace_id, _actor(user), agent_id, request.state.request_id
            )
        except AgentCatalogError as error:
            raise _as_app_error(error) from error
        return AgentDraftResponse.from_domain(draft)

    @router.put("/{agent_id}/draft", response_model=AgentDraftResponse)
    async def replace_draft(  # pyright: ignore[reportUnusedFunction]
        agent_id: UUID,
        payload: ReplaceDraftRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[AgentCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> AgentDraftResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            draft = await catalog.replace_draft(
                workspace_id,
                _actor(user),
                agent_id,
                payload.expected_revision,
                payload.spec,
                request.state.request_id,
            )
        except AgentCatalogError as error:
            raise _as_app_error(error) from error
        return AgentDraftResponse.from_domain(draft)

    @router.get("/{agent_id}/versions", response_model=list[AgentVersionResponse])
    async def list_versions(  # pyright: ignore[reportUnusedFunction]
        agent_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[AgentCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[AgentVersionResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            versions = await catalog.list_versions(
                workspace_id, _actor(user), agent_id, request.state.request_id
            )
        except AgentCatalogError as error:
            raise _as_app_error(error) from error
        return [AgentVersionResponse.from_domain(version) for version in versions]

    @router.get("/{agent_id}/versions/{version_id}", response_model=AgentVersionDetailResponse)
    async def get_version(  # pyright: ignore[reportUnusedFunction]
        agent_id: UUID,
        version_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[AgentCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> AgentVersionDetailResponse:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            version = await catalog.get_version(
                workspace_id,
                _actor(user),
                agent_id,
                version_id,
                request.state.request_id,
            )
        except AgentCatalogError as error:
            raise _as_app_error(error) from error
        return AgentVersionDetailResponse.from_domain(version)

    @router.post("/{agent_id}/publish", response_model=AgentVersionResponse)
    async def publish(  # pyright: ignore[reportUnusedFunction]
        agent_id: UUID,
        payload: PublishRequest,
        request: Request,
        response: Response,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[AgentCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> AgentVersionResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            result = await catalog.publish(
                workspace_id,
                _actor(user),
                agent_id,
                payload.expected_revision,
                request.state.request_id,
            )
        except AgentCatalogError as error:
            raise _as_app_error(error) from error
        response.status_code = (
            status.HTTP_200_OK if result.unchanged else status.HTTP_201_CREATED
        )
        return AgentVersionResponse.from_domain(result.version)

    @router.post("/{agent_id}/rollback", response_model=AgentVersionResponse)
    async def rollback(  # pyright: ignore[reportUnusedFunction]
        agent_id: UUID,
        payload: RollbackRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[AgentCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> AgentVersionResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            version = await catalog.activate_version(
                workspace_id,
                _actor(user),
                agent_id,
                payload.version_id,
                request.state.request_id,
            )
        except AgentCatalogError as error:
            raise _as_app_error(error) from error
        return AgentVersionResponse.from_domain(version)

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _as_app_error(error: AgentCatalogError) -> AppError:
    """Turn Agent Catalog refusals into Problem Details without leaking content."""
    if isinstance(error, ForbiddenAgentAction):
        return forbidden()
    if isinstance(error, UnknownAgent):
        return AppError(
            code="agent_not_found",
            title="Agent not found",
            status=404,
            detail="No such agent exists in the selected workspace.",
        )
    if isinstance(error, DraftRevisionConflict):
        return AppError(
            code="draft_revision_conflict",
            title="Draft revision conflict",
            status=409,
            detail="The agent draft changed after it was read.",
        )
    if isinstance(error, AgentAliasAlreadyUsed):
        return AppError(
            code="agent_alias_taken",
            title="Agent alias already used",
            status=409,
            detail="Another agent in this workspace already uses that alias.",
        )
    if isinstance(error, InvalidAgentAlias):
        return AppError(
            code="invalid_agent_alias",
            title="Invalid agent alias",
            status=422,
            detail="An alias uses lowercase letters, digits, and single hyphens.",
        )
    if isinstance(error, InvalidAgentName):
        return AppError(
            code="invalid_agent_name",
            title="Invalid agent name",
            status=422,
            detail="The agent name is invalid.",
        )
    if isinstance(error, ModelEndpointUnavailable):
        return AppError(
            code="model_endpoint_unavailable",
            title="Model endpoint unavailable",
            status=422,
            detail=(
                "The model endpoint this agent selects does not exist or is no "
                "longer active."
            ),
        )
    if isinstance(error, ModelOutputLimitTooHigh):
        return AppError(
            code="model_output_limit_too_high",
            title="Output limit too high",
            status=422,
            detail=(
                "The agent asks for more output than the selected endpoint "
                "produces. Lower it rather than relying on the endpoint to."
            ),
        )
    if isinstance(error, RoundCeilingExceeded):
        return AppError(
            code="round_ceiling_exceeded",
            title="Too many model rounds",
            status=422,
            # Both numbers, so the author can act on this without asking an
            # administrator what the ceiling happens to be today.
            detail=(
                f"The agent asks for {error.asked} model calls and this "
                f"platform allows {error.allowed}."
            ),
        )
    if isinstance(error, InvalidAgentSpec):
        return AppError(
            code="invalid_agent_spec",
            title="Invalid agent configuration",
            status=422,
            detail="The agent configuration is not a valid phase-two specification.",
        )
    return AppError(
        code="agent_request_rejected",
        title="Agent request rejected",
        status=422,
        detail="The agent request could not be completed.",
    )
