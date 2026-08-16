"""The two artifact endpoints: metadata, and the bytes themselves.

Both recheck Workspace membership on every request, and both answer a
cross-tenant probe with the same generic not-found a genuinely missing
artifact gets (design §6.4). No response carries an object key.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from tiny_hermes.artifacts.application.service import (
    ArtifactForbidden,
    ArtifactNotFound,
    ArtifactService,
)
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.application.machine_service import MachineIdentityService
from tiny_hermes.identity.presentation.dependencies import (
    SESSION_COOKIE,
    forbidden,
    resolve_workspace_caller,
)
from tiny_hermes.shared.errors import AppError

if TYPE_CHECKING:
    from tiny_hermes.api.resources import ApplicationResources

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]
AuthorizationHeader = Annotated[str | None, Header()]


class ArtifactResponse(BaseModel):
    id: UUID
    run_id: UUID
    session_id: UUID
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    truncated: bool
    expires_at: datetime


def _not_found() -> AppError:
    return AppError(
        code="artifact_not_found",
        title="Artifact not found",
        status=404,
        detail="No artifact by that identifier is available in this workspace.",
    )


def artifact_router(resources: "ApplicationResources") -> APIRouter:
    router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])
    auth_dependency = resources.auth_service
    machines_dependency = resources.machine_identity_service
    artifacts_dependency = resources.artifact_service

    async def _authorized(
        auth: AuthService,
        machines: MachineIdentityService,
        artifacts: ArtifactService,
        raw_workspace: str | None,
        session_token: str | None,
        authorization: str | None,
        artifact_id: UUID,
    ):  # noqa: ANN202 - Artifact, but the annotation would import the domain here
        caller = await resolve_workspace_caller(
            auth,
            machines,
            session_token=session_token,
            authorization=authorization,
            csrf_token=None,
            workspace_header=raw_workspace,
            write=False,
            required_scope="runs.read",
        )
        try:
            return await artifacts.metadata(
                caller.workspace_id, caller.actor, artifact_id
            )
        except ArtifactNotFound as missing:
            raise _not_found() from missing
        except ArtifactForbidden as refused:
            raise forbidden() from refused

    @router.get("/{artifact_id}", response_model=ArtifactResponse)
    async def artifact_metadata(  # pyright: ignore[reportUnusedFunction]
        artifact_id: UUID,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        artifacts: Annotated[
            ArtifactService, Depends(artifacts_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        authorization: AuthorizationHeader = None,
    ) -> ArtifactResponse:
        found = await _authorized(
            auth,
            machines,
            artifacts,
            selected_workspace,
            session_token,
            authorization,
            artifact_id,
        )
        return ArtifactResponse(
            id=found.id,
            run_id=found.run_id,
            session_id=found.session_id,
            filename=found.filename,
            media_type=found.media_type,
            size_bytes=found.size_bytes,
            sha256=found.sha256,
            truncated=found.truncated,
            expires_at=found.expires_at,
        )

    @router.get("/{artifact_id}/content")
    async def artifact_content(  # pyright: ignore[reportUnusedFunction]
        artifact_id: UUID,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        machines: Annotated[
            MachineIdentityService, Depends(machines_dependency, scope="function")
        ],
        artifacts: Annotated[
            ArtifactService, Depends(artifacts_dependency, scope="function")
        ],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        authorization: AuthorizationHeader = None,
    ) -> StreamingResponse:
        found = await _authorized(
            auth,
            machines,
            artifacts,
            selected_workspace,
            session_token,
            authorization,
            artifact_id,
        )
        return StreamingResponse(
            artifacts.content(found),
            media_type=found.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{found.filename}"',
                "Content-Length": str(found.size_bytes),
            },
        )

    return router
