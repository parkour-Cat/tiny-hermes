from collections.abc import Callable

from fastapi import APIRouter, Response, status


def health_router(readiness: Callable[[], bool]) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/health/live")
    def live() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "alive"}

    @router.get("/health/ready")
    def ready(response: Response) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        if not readiness():
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready"}
        return {"status": "ready"}

    return router
