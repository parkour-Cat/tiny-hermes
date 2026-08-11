from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.auth_service import (
    AuthService,
    BootstrapClosed,
    InvalidBootstrapToken,
    InvalidCredentials,
)
from tiny_hermes.identity.domain.models import AuthenticatedUser, NewLocalUser
from tiny_hermes.identity.presentation.dependencies import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    csrf_failed,
    unauthenticated,
)
from tiny_hermes.shared.errors import AppError


class BootstrapRequest(BaseModel):
    subject: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    subject: EmailStr
    password: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    id: UUID
    subject: str
    display_name: str
    status: str
    is_platform_admin: bool

    @classmethod
    def from_domain(cls, user: AuthenticatedUser) -> "UserResponse":
        return cls(
            id=user.id,
            subject=user.subject,
            display_name=user.display_name,
            status=user.status,
            is_platform_admin=user.is_platform_admin,
        )


def identity_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["identity"])
    service_dependency = resources.auth_service

    @router.post("/bootstrap", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
    async def bootstrap(  # pyright: ignore[reportUnusedFunction]
        payload: BootstrapRequest,
        request: Request,
        bootstrap_token: Annotated[str, Header(alias="X-Bootstrap-Token")],
        service: Annotated[AuthService, Depends(service_dependency, scope="function")],
    ) -> UserResponse:
        try:
            user = await service.bootstrap(
                bootstrap_token,
                NewLocalUser(str(payload.subject), payload.display_name, payload.password),
                request.state.request_id,
            )
        except InvalidBootstrapToken as error:
            raise AppError(
                code="invalid_bootstrap_token",
                title="Invalid bootstrap token",
                status=403,
                detail="The bootstrap token is invalid.",
            ) from error
        except BootstrapClosed as error:
            raise AppError(
                code="bootstrap_closed",
                title="Bootstrap closed",
                status=409,
                detail="The first platform administrator already exists.",
            ) from error
        return UserResponse.from_domain(user)

    @router.post(
        "/auth/sessions", response_model=UserResponse, status_code=status.HTTP_201_CREATED
    )
    async def login(  # pyright: ignore[reportUnusedFunction]
        payload: LoginRequest,
        response: Response,
        request: Request,
        service: Annotated[AuthService, Depends(service_dependency, scope="function")],
    ) -> UserResponse:
        try:
            session_token, csrf_token, user = await service.login(
                str(payload.subject), payload.password, request.state.request_id
            )
        except InvalidCredentials as error:
            raise AppError(
                code="invalid_credentials",
                title="Invalid credentials",
                status=401,
                detail="The local account or password is invalid.",
            ) from error
        secure = resources.settings.environment != "development"
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            max_age=resources.settings.session_ttl_seconds,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=resources.settings.session_ttl_seconds,
            httponly=False,
            secure=secure,
            samesite="lax",
            path="/",
        )
        return UserResponse.from_domain(user)

    @router.get("/auth/me", response_model=UserResponse)
    async def me(  # pyright: ignore[reportUnusedFunction]
        service: Annotated[AuthService, Depends(service_dependency, scope="function")],
        session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> UserResponse:
        if not session_token:
            raise unauthenticated()
        try:
            user = await service.authenticate(session_token)
        except InvalidCredentials as error:
            raise unauthenticated() from error
        return UserResponse.from_domain(user)

    @router.delete("/auth/sessions/current", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(  # pyright: ignore[reportUnusedFunction]
        response: Response,
        request: Request,
        service: Annotated[AuthService, Depends(service_dependency, scope="function")],
        session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> None:
        if not session_token:
            raise unauthenticated()
        if not csrf_token:
            raise csrf_failed()
        try:
            await service.verify_csrf(session_token, csrf_token)
        except InvalidCredentials as error:
            raise csrf_failed() from error
        await service.logout(session_token, request.state.request_id)
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")

    return router
