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
from tiny_hermes.http_tools.presentation.routes import http_tool_router
from tiny_hermes.identity.presentation.end_user_dependencies import reject_end_user_caller
from tiny_hermes.identity.presentation.end_user_routes import (
    end_user_console_router,
    end_user_router,
)
from tiny_hermes.identity.presentation.machine_routes import machine_router
from tiny_hermes.identity.presentation.routes import identity_router
from tiny_hermes.mcp.presentation.routes import mcp_router
from tiny_hermes.memory.presentation.routes import memory_router
from tiny_hermes.memory.presentation.subject_routes import subject_router
from tiny_hermes.model_catalog.presentation.pricing_routes import pricing_router
from tiny_hermes.model_catalog.presentation.routes import model_endpoint_router
from tiny_hermes.outbound.presentation.routes import outbound_scope_router
from tiny_hermes.runs.presentation.approval_routes import approval_router
from tiny_hermes.runs.presentation.completions import completions_router
from tiny_hermes.runs.presentation.events import run_event_router
from tiny_hermes.runs.presentation.routes import run_router, session_router
from tiny_hermes.secrets.presentation.routes import secret_router
from tiny_hermes.shared.config import Settings
from tiny_hermes.shared.errors import AppError
from tiny_hermes.skills.presentation.routes import skill_proposal_router, skill_router
from tiny_hermes.tenancy.presentation.routes import workspace_router

#: Design §8's last row, applied once here rather than as a branch in
#: `resolve_workspace_caller` or a dependency repeated in every router below.
#: `include_router(..., dependencies=[...])` runs this ahead of every route a
#: console router defines, today and in whatever gets added to these routers
#: later — which is what makes "no exceptions" true without a per-router
#: opt-in that a future router could forget.
_CONSOLE_ONLY = [Depends(reject_end_user_caller)]


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
    app.include_router(health_router(selected_readiness))
    # End-user entry design §4.5's first sentence: an end user never reaches
    # any of these. Every router below is a console capability and carries
    # the guard, including `end_user_console_router` — this task's own
    # admin routes are not exempt just because they live next to the end
    # user's entry point in the same module. `end_user_router` is the one
    # router that must not carry it, since it is that entry point.
    app.include_router(identity_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(machine_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(workspace_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(agent_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(model_endpoint_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(session_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(run_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(run_event_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(completions_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(artifact_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(secret_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(outbound_scope_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(skill_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(http_tool_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(approval_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(memory_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(subject_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(mcp_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(pricing_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(skill_proposal_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(end_user_console_router(resources), dependencies=_CONSOLE_ONLY)
    app.include_router(end_user_router(resources))
    return app


def _migration_head() -> str:
    revision = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    if revision is None:
        raise RuntimeError("The migration history has no head revision")
    return revision


app = create_app()
