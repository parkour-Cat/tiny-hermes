import asyncio

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.domain.models import NewLocalUser
from tiny_hermes.identity.infrastructure.sql_store import SqlAuthStore
from tiny_hermes.identity.infrastructure.tables import UserRow


async def test_only_one_concurrent_bootstrap_commits(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE audit_events, auth_sessions, auth_identities, users CASCADE")
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def attempt(index: int) -> object:
        async with factory.begin() as session:
            service = AuthService(SqlAuthStore(session), "a" * 32, 28_800)
            return await service.bootstrap(
                "a" * 32,
                NewLocalUser(f"admin-{index}@example.com", f"Admin {index}", "long-pass-123"),
                f"req-{index}",
            )

    results = await asyncio.gather(attempt(1), attempt(2), return_exceptions=True)

    assert sum(not isinstance(value, Exception) for value in results) == 1
    async with factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(UserRow).where(UserRow.is_platform_admin.is_(True))
        )
    assert count == 1
