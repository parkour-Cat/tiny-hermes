from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tiny_hermes.api.health import health_router
from tiny_hermes.api.request_context import RequestIdMiddleware
from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.presentation.routes import identity_router
from tiny_hermes.shared.config import Settings
from tiny_hermes.shared.errors import AppError
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
    readiness: Callable[[], bool] = lambda: True,
    *,
    settings: Settings | None = None,
) -> FastAPI:
    resources = ApplicationResources(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        await resources.close()

    app = FastAPI(title="tiny-hermes API", version="0.0.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(health_router(readiness))
    app.include_router(identity_router(resources))
    app.include_router(workspace_router(resources))
    return app


app = create_app()
