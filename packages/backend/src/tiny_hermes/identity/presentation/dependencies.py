from uuid import UUID

from tiny_hermes.identity.application.auth_service import AuthService, InvalidCredentials
from tiny_hermes.identity.domain.models import AuthenticatedUser
from tiny_hermes.shared.errors import AppError

SESSION_COOKIE = "tiny_hermes_session"
CSRF_COOKIE = "tiny_hermes_csrf"


async def authenticate_browser_user(
    auth: AuthService, session_token: str | None
) -> AuthenticatedUser:
    if not session_token:
        raise unauthenticated()
    try:
        return await auth.authenticate(session_token)
    except InvalidCredentials as error:
        raise unauthenticated() from error


async def verify_browser_write(
    auth: AuthService, session_token: str | None, csrf_token: str | None
) -> AuthenticatedUser:
    if not session_token:
        raise unauthenticated()
    if not csrf_token:
        raise csrf_failed()
    try:
        return await auth.verify_csrf(session_token, csrf_token)
    except InvalidCredentials as error:
        raise csrf_failed() from error


def require_workspace_id(raw_value: str | None) -> UUID:
    if raw_value is None:
        raise AppError(
            code="workspace_required",
            title="Workspace required",
            status=400,
            detail="X-Workspace-Id is required for workspace-scoped requests.",
        )
    try:
        return UUID(raw_value)
    except ValueError as error:
        raise AppError(
            code="invalid_workspace_id",
            title="Invalid workspace identifier",
            status=400,
            detail="X-Workspace-Id must be a UUID.",
        ) from error


def unauthenticated() -> AppError:
    return AppError(
        code="unauthenticated",
        title="Authentication required",
        status=401,
        detail="A valid browser session is required.",
    )


def csrf_failed() -> AppError:
    return AppError(
        code="csrf_failed",
        title="Request verification failed",
        status=403,
        detail="The request verification token is missing or invalid.",
    )


def forbidden() -> AppError:
    return AppError(
        code="forbidden",
        title="Forbidden",
        status=403,
        detail="The current user cannot perform this workspace action.",
    )
