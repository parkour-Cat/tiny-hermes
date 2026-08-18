from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Request
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
from tiny_hermes.identity.presentation.machine_routes import machine_router
from tiny_hermes.identity.presentation.routes import identity_router
from tiny_hermes.model_catalog.presentation.routes import model_endpoint_router
from tiny_hermes.outbound.presentation.routes import outbound_scope_router
from tiny_hermes.runs.presentation.completions import completions_router
from tiny_hermes.runs.presentation.events import run_event_router
from tiny_hermes.runs.presentation.routes import run_router, session_router
from tiny_hermes.secrets.presentation.routes import secret_router
from tiny_hermes.shared.config import Settings
from tiny_hermes.shared.errors import AppError
from tiny_hermes.skills.presentation.routes import skill_proposal_router, skill_router
from tiny_hermes.tenancy.presentation.routes import workspace_router


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
    app.include_router(identity_router(resources))
    app.include_router(machine_router(resources))
    app.include_router(workspace_router(resources))
    app.include_router(agent_router(resources))
    app.include_router(model_endpoint_router(resources))
    app.include_router(session_router(resources))
    app.include_router(run_router(resources))
    app.include_router(run_event_router(resources))
    app.include_router(completions_router(resources))
    app.include_router(artifact_router(resources))
    app.include_router(secret_router(resources))
    app.include_router(outbound_scope_router(resources))
    app.include_router(skill_router(resources))
    app.include_router(skill_proposal_router(resources))
    return app


def _migration_head() -> str:
    revision = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    if revision is None:
        raise RuntimeError("The migration history has no head revision")
    return revision


app = create_app()
