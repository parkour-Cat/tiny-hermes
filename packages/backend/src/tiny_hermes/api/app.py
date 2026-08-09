from collections.abc import Callable

from fastapi import FastAPI

from tiny_hermes.api.health import health_router


def create_app(readiness: Callable[[], bool] = lambda: True) -> FastAPI:
    app = FastAPI(title="tiny-hermes API", version="0.0.0")
    app.include_router(health_router(readiness))
    return app


app = create_app()
