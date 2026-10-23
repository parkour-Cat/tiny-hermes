from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tiny_hermes.shared.config import Settings


class Base(DeclarativeBase):
    pass


class IdMixin:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


def build_session_factory(
    settings: Settings,
    *,
    pool_size: int = 5,
    max_overflow: int = 10,
    info: dict[str, Any] | None = None,
) -> async_sessionmaker[AsyncSession]:
    # The defaults are SQLAlchemy's own, which every caller had before a Worker
    # process could hold more than one Worker.
    engine = application_engine(settings, pool_size=pool_size, max_overflow=max_overflow)
    return async_sessionmaker(engine, expire_on_commit=False, info=info or {})


def application_engine(settings: Settings, *, pool_size: int, max_overflow: int) -> AsyncEngine:
    """An engine whose every connection acts as `DATABASE_ROLE` (§9.3, v2.20).

    Row-level security does not apply to a superuser, to a table's owner, or
    to a role with BYPASSRLS, and the login Compose ships is all three. So each
    connection steps down to a role that is none of them as soon as it opens.
    Empty leaves the login as it is: for a deployment that logs in as such a
    role directly.
    """
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )
    role = settings.database_role
    if role:
        statement = f'SET ROLE "{role}"'

        async def step_down(raw: Any) -> None:
            await raw.execute(statement)

        def assume_role(dbapi_connection: Any, _: Any) -> None:
            dbapi_connection.run_async(step_down)

        event.listen(engine.sync_engine, "connect", assume_role)
    return engine


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
