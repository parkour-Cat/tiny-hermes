from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

ReadinessChecks = dict[str, str]
ReadinessCheck = Callable[[], Awaitable[ReadinessChecks]]


class DatabaseReadinessProbe:
    def __init__(
        self, engine: Callable[[], AsyncEngine], expected_migration_revision: str
    ) -> None:
        self._engine = engine
        self._expected_migration_revision = expected_migration_revision

    async def __call__(self) -> ReadinessChecks:
        try:
            async with self._engine().connect() as connection:
                await connection.execute(text("SELECT 1"))
                try:
                    revision = await connection.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                except Exception:
                    return {"database": "ok", "migration": "behind"}
        except Exception:
            return {"database": "failed"}

        migration = (
            "current" if revision == self._expected_migration_revision else "behind"
        )
        return {"database": "ok", "migration": migration}


def health_router(readiness: ReadinessCheck) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/health/live")
    def live() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "alive"}

    @router.get("/health/ready")
    async def ready(  # pyright: ignore[reportUnusedFunction]
        response: Response,
    ) -> dict[str, str | ReadinessChecks]:
        checks = await readiness()
        is_ready = (
            checks.get("database") == "ok" and checks.get("migration") == "current"
        )
        if not is_ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready", "checks": checks}
        return {"status": "ready", "checks": checks}

    return router
