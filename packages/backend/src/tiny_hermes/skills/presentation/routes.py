"""The catalog's HTTP face.

`CreateSkillRequest` and `UploadVersionRequest` take `[{path, content}]`. That
is the whole of red line three on the manual path: the browser has a directory
picker, so no endpoint here ever receives an archive and the server never grows
a face that unpacks one. §4's git import reads a tarball as a stream and still
never writes one to disk.

The one unusual status code is on `POST /{skill_id}/versions`: 201 when a
version was created, 200 when the same content was already a version. Uploading
an unchanged directory is not a publication.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
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
from tiny_hermes.shared.errors import AppError
from tiny_hermes.skills.application.service import (
    ForbiddenSkillAction,
    InvalidSkillPackage,
    SkillCatalog,
    SkillNameMismatch,
    SkillNameTaken,
    SkillScanRefused,
    UnknownSkill,
    UnknownSkillVersion,
    VersionNotBindable,
)
from tiny_hermes.skills.domain.models import Skill, SkillScope, SkillVersion
from tiny_hermes.skills.domain.package import (
    MAX_FILE_BYTES,
    MAX_FILES,
    SkillFile,
)
from tiny_hermes.skills.domain.scan import Finding
from tiny_hermes.tenancy.domain.models import Actor

WorkspaceHeader = Annotated[str | None, Header(alias="X-Workspace-Id")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


class SkillFilePayload(BaseModel):
    """One file, as the directory picker read it."""

    path: str = Field(min_length=1, max_length=512)
    #: Bounded here as characters and again in the domain as UTF-8 bytes. This
    #: one stops a body large enough to be worth parsing from being parsed.
    content: str = Field(max_length=MAX_FILE_BYTES)


class CreateSkillRequest(BaseModel):
    scope: Literal["workspace", "platform"]
    files: list[SkillFilePayload] = Field(min_length=1, max_length=MAX_FILES)


class UploadVersionRequest(BaseModel):
    files: list[SkillFilePayload] = Field(min_length=1, max_length=MAX_FILES)


class SetCurrentVersionRequest(BaseModel):
    version_id: UUID


class SkillResponse(BaseModel):
    id: UUID
    scope: str
    workspace_id: UUID | None
    name: str
    current_version_id: UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, skill: Skill) -> "SkillResponse":
        return cls(
            id=skill.id,
            scope=skill.scope.value,
            workspace_id=skill.workspace_id,
            name=skill.name,
            current_version_id=skill.current_version_id,
            created_at=skill.created_at,
            updated_at=skill.updated_at,
        )


class FindingResponse(BaseModel):
    code: str
    severity: str
    path: str
    detail: str

    @classmethod
    def from_domain(cls, finding: Finding) -> "FindingResponse":
        return cls(
            code=finding.code,
            severity=finding.severity.value,
            path=finding.path,
            detail=finding.detail,
        )


class SkillVersionResponse(BaseModel):
    id: UUID
    skill_id: UUID
    version_number: int
    content_hash: str
    name: str
    description: str
    findings: list[FindingResponse]
    source: str
    source_url: str | None
    source_ref: str | None
    status: str
    bindable: bool
    created_at: datetime

    @classmethod
    def from_domain(cls, version: SkillVersion) -> "SkillVersionResponse":
        return cls(
            id=version.id,
            skill_id=version.skill_id,
            version_number=version.version_number,
            content_hash=version.content_hash,
            name=version.manifest.name,
            description=version.manifest.description,
            findings=[FindingResponse.from_domain(item) for item in version.findings],
            source=version.source.value,
            source_url=version.source_url,
            source_ref=version.source_ref,
            status=version.status.value,
            bindable=version.bindable,
            created_at=version.created_at,
        )


class SkillVersionDetailResponse(SkillVersionResponse):
    files: list[SkillFilePayload]


def skill_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/skills", tags=["skills"])
    auth_dependency = resources.auth_service
    catalog_dependency = resources.skill_catalog

    @router.get("", response_model=list[SkillResponse])
    async def list_skills(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[SkillCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[SkillResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            listed = await catalog.list_skills(
                _actor(user), workspace_id, request.state.request_id
            )
        except ForbiddenSkillAction as error:
            raise forbidden() from error
        return [SkillResponse.from_domain(skill) for skill in listed]

    @router.post("", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
    async def create_skill(  # pyright: ignore[reportUnusedFunction]
        payload: CreateSkillRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[SkillCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> SkillResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            skill, _ = await catalog.create_skill(
                _actor(user),
                workspace_id,
                SkillScope(payload.scope),
                _files(payload.files),
                request.state.request_id,
            )
        except ForbiddenSkillAction as error:
            raise forbidden() from error
        except SkillNameTaken as error:
            raise AppError(
                code="skill_name_taken",
                title="Skill name taken",
                status=409,
                detail="A skill by that name already exists at this level.",
            ) from error
        except InvalidSkillPackage as error:
            raise _invalid_package(error) from error
        except SkillScanRefused as error:
            raise _scan_refused(error) from error
        return SkillResponse.from_domain(skill)

    @router.get("/{skill_id}", response_model=SkillResponse)
    async def get_skill(  # pyright: ignore[reportUnusedFunction]
        skill_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[SkillCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> SkillResponse:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            skill = await catalog.get_skill(
                _actor(user), workspace_id, skill_id, request.state.request_id
            )
        except ForbiddenSkillAction as error:
            raise forbidden() from error
        except UnknownSkill as error:
            raise _skill_not_found() from error
        return SkillResponse.from_domain(skill)

    @router.get("/{skill_id}/versions", response_model=list[SkillVersionResponse])
    async def list_versions(  # pyright: ignore[reportUnusedFunction]
        skill_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[SkillCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> list[SkillVersionResponse]:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            versions = await catalog.list_versions(
                _actor(user), workspace_id, skill_id, request.state.request_id
            )
        except ForbiddenSkillAction as error:
            raise forbidden() from error
        except UnknownSkill as error:
            raise _skill_not_found() from error
        return [SkillVersionResponse.from_domain(version) for version in versions]

    @router.post(
        "/{skill_id}/versions",
        response_model=SkillVersionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_version(  # pyright: ignore[reportUnusedFunction]
        skill_id: UUID,
        payload: UploadVersionRequest,
        request: Request,
        response: Response,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[SkillCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> SkillVersionResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            result = await catalog.upload_version(
                _actor(user),
                workspace_id,
                skill_id,
                _files(payload.files),
                request.state.request_id,
            )
        except ForbiddenSkillAction as error:
            raise forbidden() from error
        except UnknownSkill as error:
            raise _skill_not_found() from error
        except InvalidSkillPackage as error:
            raise _invalid_package(error) from error
        except SkillNameMismatch as error:
            raise AppError(
                code="skill_name_mismatch",
                title="Skill name mismatch",
                status=422,
                detail=(
                    f"This skill is named {error.expected!r}, "
                    f"but the uploaded SKILL.md names {error.found!r}."
                ),
            ) from error
        except SkillScanRefused as error:
            raise _scan_refused(error) from error
        if not result.created:
            # Same content, same version, nothing published. 200 rather than
            # 201 is the roadmap's exit check for re-import spoken in HTTP.
            response.status_code = status.HTTP_200_OK
        return SkillVersionResponse.from_domain(result.version)

    @router.get(
        "/{skill_id}/versions/{version_id}", response_model=SkillVersionDetailResponse
    )
    async def read_version(  # pyright: ignore[reportUnusedFunction]
        skill_id: UUID,
        version_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[SkillCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
    ) -> SkillVersionDetailResponse:
        user = await authenticate_browser_user(auth, session_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            version, files = await catalog.read_version(
                _actor(user), workspace_id, skill_id, version_id, request.state.request_id
            )
        except ForbiddenSkillAction as error:
            raise forbidden() from error
        except UnknownSkill as error:
            raise _skill_not_found() from error
        except UnknownSkillVersion as error:
            raise _version_not_found() from error
        return SkillVersionDetailResponse(
            **SkillVersionResponse.from_domain(version).model_dump(),
            files=[
                SkillFilePayload(path=entry.path, content=entry.text) for entry in files
            ],
        )

    @router.post(
        "/{skill_id}/versions/{version_id}/withdraw",
        response_model=SkillVersionResponse,
    )
    async def withdraw_version(  # pyright: ignore[reportUnusedFunction]
        skill_id: UUID,
        version_id: UUID,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[SkillCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> SkillVersionResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            withdrawn = await catalog.withdraw_version(
                _actor(user), workspace_id, skill_id, version_id, request.state.request_id
            )
        except ForbiddenSkillAction as error:
            raise forbidden() from error
        except UnknownSkill as error:
            raise _skill_not_found() from error
        except UnknownSkillVersion as error:
            raise _version_not_found() from error
        return SkillVersionResponse.from_domain(withdrawn)

    @router.put("/{skill_id}/current-version", response_model=SkillResponse)
    async def set_current_version(  # pyright: ignore[reportUnusedFunction]
        skill_id: UUID,
        payload: SetCurrentVersionRequest,
        request: Request,
        auth: Annotated[AuthService, Depends(auth_dependency, scope="function")],
        catalog: Annotated[SkillCatalog, Depends(catalog_dependency, scope="function")],
        selected_workspace: WorkspaceHeader = None,
        session_token: SessionCookie = None,
        csrf_token: CsrfHeader = None,
    ) -> SkillResponse:
        user = await verify_browser_write(auth, session_token, csrf_token)
        workspace_id = require_workspace_id(selected_workspace)
        try:
            skill = await catalog.set_current_version(
                _actor(user),
                workspace_id,
                skill_id,
                payload.version_id,
                request.state.request_id,
            )
        except ForbiddenSkillAction as error:
            raise forbidden() from error
        except UnknownSkill as error:
            raise _skill_not_found() from error
        except UnknownSkillVersion as error:
            raise _version_not_found() from error
        except VersionNotBindable as error:
            raise AppError(
                code="skill_version_not_bindable",
                title="Version not bindable",
                status=409,
                detail=(
                    "That version is withdrawn or blocked, so it cannot be "
                    "where new bindings start."
                ),
            ) from error
        return SkillResponse.from_domain(skill)

    return router


def _actor(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def _files(payload: list[SkillFilePayload]) -> tuple[SkillFile, ...]:
    return tuple(SkillFile(path=item.path, text=item.content) for item in payload)


def _skill_not_found() -> AppError:
    return AppError(
        code="skill_not_found",
        title="Skill not found",
        status=404,
        detail="No skill by that identifier is available from this workspace.",
    )


def _version_not_found() -> AppError:
    return AppError(
        code="skill_version_not_found",
        title="Skill version not found",
        status=404,
        detail="That skill has no version by that identifier.",
    )


def _invalid_package(error: InvalidSkillPackage) -> AppError:
    return AppError(
        code="invalid_skill_package",
        title="Invalid skill package",
        status=422,
        detail=error.reason,
    )


def _scan_refused(error: SkillScanRefused) -> AppError:
    """Every blocking finding travels back, path and all.

    An author told only that the upload was refused has to guess which of forty
    files holds the key somebody pasted into it.
    """
    return AppError(
        code="skill_scan_refused",
        title="Skill refused by the scan",
        status=422,
        detail="The static scan found content that cannot be stored.",
        context={
            "findings": [
                {
                    "code": finding.code,
                    "severity": finding.severity.value,
                    "path": finding.path,
                    "detail": finding.detail,
                }
                for finding in error.findings
            ]
        },
    )
