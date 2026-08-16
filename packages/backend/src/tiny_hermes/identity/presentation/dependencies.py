from dataclasses import dataclass
from uuid import UUID

from tiny_hermes.identity.application.auth_service import AuthService, InvalidCredentials
from tiny_hermes.identity.application.machine_service import (
    InvalidApiKey,
    MachineIdentityService,
    WorkspaceBindingMismatch,
)
from tiny_hermes.identity.domain.models import AuthenticatedUser
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

SESSION_COOKIE = "tiny_hermes_session"
CSRF_COOKIE = "tiny_hermes_csrf"


@dataclass(frozen=True)
class WorkspaceCaller:
    actor: Actor
    workspace_id: UUID


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


def unauthenticated_bearer() -> AppError:
    return AppError(
        code="unauthenticated",
        title="Authentication required",
        status=401,
        detail="A valid API key is required.",
    )


def unauthenticated_any() -> AppError:
    return AppError(
        code="unauthenticated",
        title="Authentication required",
        status=401,
        detail="A valid browser session or API key is required.",
    )


def parse_bearer_token(authorization: str | None) -> str | None:
    if authorization is None or not authorization.strip():
        return None
    scheme, separator, credential = authorization.partition(" ")
    if separator == "" or scheme.lower() != "bearer" or not credential.strip():
        raise unauthenticated_bearer()
    return credential.strip()


def optional_workspace_id(raw_value: str | None) -> UUID | None:
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return UUID(raw_value)
    except ValueError as error:
        raise AppError(
            code="invalid_workspace_id",
            title="Invalid workspace identifier",
            status=400,
            detail="X-Workspace-Id must be a UUID.",
        ) from error


def require_scope(actor: Actor, scope: str | None) -> None:
    if scope is None or actor.scopes is None:
        return
    if scope not in actor.scopes:
        raise forbidden()


async def resolve_api_key_caller(
    machines: MachineIdentityService,
    *,
    authorization: str | None,
    workspace_header: str | None,
    required_scope: str | None,
) -> WorkspaceCaller:
    """Bearer only. A cookie is never a substitute on Chat Completions."""
    token = parse_bearer_token(authorization)
    if token is None:
        raise unauthenticated_bearer()
    try:
        machine = await machines.authenticate(
            token, optional_workspace_id(workspace_header)
        )
    except InvalidApiKey as error:
        raise unauthenticated_bearer() from error
    except WorkspaceBindingMismatch as error:
        raise forbidden() from error
    actor = Actor(
        machine.account.id,
        False,
        is_service_account=True,
        role=machine.account.role,
        scopes=machine.scopes,
    )
    require_scope(actor, required_scope)
    return WorkspaceCaller(actor, machine.account.workspace_id)


async def resolve_workspace_caller(
    auth: AuthService,
    machines: MachineIdentityService,
    *,
    session_token: str | None,
    authorization: str | None,
    csrf_token: str | None,
    workspace_header: str | None,
    write: bool,
    required_scope: str | None,
) -> WorkspaceCaller:
    """Cookie XOR Bearer. CSRF only when the caller is a browser."""
    token = parse_bearer_token(authorization)
    if token is not None:
        try:
            machine = await machines.authenticate(
                token, optional_workspace_id(workspace_header)
            )
        except InvalidApiKey as error:
            raise unauthenticated_bearer() from error
        except WorkspaceBindingMismatch as error:
            raise forbidden() from error
        actor = Actor(
            machine.account.id,
            False,
            is_service_account=True,
            role=machine.account.role,
            scopes=machine.scopes,
        )
        require_scope(actor, required_scope)
        return WorkspaceCaller(actor, machine.account.workspace_id)

    if write:
        user = await verify_browser_write(auth, session_token, csrf_token)
    else:
        if not session_token:
            raise unauthenticated_any()
        user = await authenticate_browser_user(auth, session_token)
    return WorkspaceCaller(
        Actor(user.id, user.is_platform_admin),
        require_workspace_id(workspace_header),
    )
