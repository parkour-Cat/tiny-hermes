from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from tiny_hermes.agents.presentation.routes import agent_router
from tiny_hermes.api.health import (
    ApiReadinessProbe,
    DatabaseReadinessProbe,
    ReadinessCheck,
    health_router,
)
from tiny_hermes.api.request_context import RequestIdMiddleware
from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.artifacts.presentation.routes import artifact_router
from tiny_hermes.audit.presentation.routes import audit_router
from tiny_hermes.channels.presentation.routes import (
    channel_binding_router,
    feishu_webhook_router,
)
from tiny_hermes.http_tools.presentation.routes import http_tool_router
from tiny_hermes.identity.presentation.end_user_dependencies import reject_end_user_caller
from tiny_hermes.identity.presentation.end_user_routes import (
    end_user_console_router,
    end_user_router,
)
from tiny_hermes.identity.presentation.machine_routes import machine_router
from tiny_hermes.identity.presentation.oidc_routes import oidc_router
from tiny_hermes.identity.presentation.routes import identity_router
from tiny_hermes.mcp.presentation.routes import mcp_router
from tiny_hermes.memory.presentation.end_user_subject_routes import end_user_subject_router
from tiny_hermes.memory.presentation.routes import memory_router
from tiny_hermes.memory.presentation.subject_routes import subject_router
from tiny_hermes.model_catalog.presentation.pricing_routes import pricing_router
from tiny_hermes.model_catalog.presentation.routes import model_endpoint_router
from tiny_hermes.outbound.presentation.routes import outbound_scope_router
from tiny_hermes.runs.presentation.approval_routes import approval_router
from tiny_hermes.runs.presentation.completions import completions_router
from tiny_hermes.runs.presentation.end_user_approval_routes import end_user_approval_router
from tiny_hermes.runs.presentation.end_user_routes import end_user_run_router
from tiny_hermes.runs.presentation.events import run_event_router
from tiny_hermes.runs.presentation.routes import run_router, session_router, usage_router
from tiny_hermes.secrets.presentation.routes import secret_router
from tiny_hermes.shared.config import Settings
from tiny_hermes.shared.errors import AppError, AuditedDenial
from tiny_hermes.skills.presentation.routes import skill_proposal_router, skill_router
from tiny_hermes.tenancy.presentation.routes import workspace_router

#: Design §8's last row, applied once here rather than as a branch in
#: `resolve_workspace_caller` or a dependency repeated in every router below.
#: `include_router(..., dependencies=[...])` runs this ahead of every route a
#: console router defines, today and in whatever gets added to these routers
#: later — which is what makes "no exceptions" true without a per-router
#: opt-in that a future router could forget.
_CONSOLE_ONLY = [Depends(reject_end_user_caller)]


async def audited_denial_handler(request: Request, error: Exception) -> JSONResponse:
    """Answer a refusal whose audit row has already been committed."""
    from tiny_hermes.agents.application.service import AgentCatalogError
    from tiny_hermes.agents.presentation.routes import as_app_error

    if isinstance(error, AgentCatalogError):
        return await app_error_handler(request, as_app_error(error))
    raise error  # pragma: no cover - every AuditedDenial today is one of these


async def app_error_handler(request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, AppError):
        raise error
    return JSONResponse(
        status_code=error.status,
        media_type="application/problem+json",
        content={
            "type": f"https://tiny-hermes.dev/errors/{error.code.replace('_', '-')}",
            "code": error.code,
            "title": error.title,
            "status": error.status,
            "detail": error.detail,
            "request_id": request.state.request_id,
            "context": error.context,
        },
    )


def create_app(
    readiness: ReadinessCheck | None = None,
    *,
    settings: Settings | None = None,
) -> FastAPI:
    resources = ApplicationResources(settings)
    selected_readiness = readiness or ApiReadinessProbe(
        DatabaseReadinessProbe(resources.database_engine, _migration_head()),
        lambda: resources.settings.tiny_hermes_kek,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        await resources.close()

    app = FastAPI(title="tiny-hermes API", version="0.0.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(AppError, app_error_handler)
    # An `AuditedDenial` reaches here only because its route let it through
    # rather than converting it, which is what gives the request dependency
    # a chance to commit the security audit row the refusal wrote (§23
    # assertions 2 and 14). By this point that commit has happened, so all
    # that is left is to answer the caller — with exactly the response the
    # route would have produced had it converted the error itself.
    app.add_exception_handler(AuditedDenial, audited_denial_handler)
    app.include_router(health_router(selected_readiness))
    # End-user entry design §4.5's first sentence: an end user never reaches
    # any of these. Every router below is a console capability and carries
    # the guard, including `end_user_console_router` — this task's own
    # admin routes are not exempt just because they live next to the end
    # user's entry point in the same module. `end_user_router` is the one
    # router that must not carry it, since it is that entry point.
    app.include_router(identity_router(resources), dependencies=_CONSOLE_ONLY)
    # OIDC login design §2's own routes are anonymous the same way
    # `identity_router`'s bootstrap/login are — a platform member has no
    # console session yet when they reach `/auth/oidc/{id}/start` — so this
    # is `_CONSOLE_ONLY` (not an end user's entry point) rather than exempt.
    app.include_router(oidc_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(machine_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(workspace_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(agent_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(model_endpoint_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(session_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(run_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(usage_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(run_event_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(completions_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(artifact_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(secret_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(outbound_scope_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(skill_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(http_tool_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(approval_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(audit_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(memory_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(subject_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(mcp_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(pricing_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(skill_proposal_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(end_user_console_router(resources), dependencies=_CONSOLE_ONLY)
    # Not console-only: Feishu holds no credential of this platform's and
    # arrives with no cookies, so the guard that refuses an end-user cookie
    # has nothing to say about it. The signature over the body is what makes
    # this door safe (see the router's own docstring).
    app.include_router(feishu_webhook_router(resources))
    # Console-only: managing a binding is §4.6's `密钥、安全策略与渠道`,
    # while the delivery route above is unauthenticated by necessity.
    app.include_router(channel_binding_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(end_user_router(resources))
    # §5's own entry point: an end user running an Agent. Never `_CONSOLE_
    # ONLY`, for the same reason `end_user_router` above is not — every
    # route in it authenticates with `resolve_end_user_caller`, not a
    # workspace Role, so there is no console session for the guard to reject
    # in the first place.
    app.include_router(end_user_run_router(resources))
    # §4.6's matrix, 仅发起人本人 row: the end user who owns a
    # `user_confirmation` answers it here, never through `approval_router`
    # above, which is `_CONSOLE_ONLY` and would refuse the cookie this needs.
    app.include_router(end_user_approval_router(resources))
    # §4.6's "本人" row for an end user — see the module's own docstring for
    # why this cannot be `subject_router` with a second auth branch.
    app.include_router(end_user_subject_router(resources))
    return app


def _migration_head() -> str:
    revision = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    if revision is None:
        raise RuntimeError("The migration history has no head revision")
    return revision


app = create_app()
