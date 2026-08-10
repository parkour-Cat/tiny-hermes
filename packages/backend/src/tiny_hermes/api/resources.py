from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tiny_hermes.agents.application.service import AgentCatalog
from tiny_hermes.agents.infrastructure.sql_store import SqlAgentStore
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.infrastructure.sql_store import SqlAuthStore
from tiny_hermes.shared.config import Settings, get_settings
from tiny_hermes.shared.errors import AppError, AuditedDenial
from tiny_hermes.tenancy.application.workspace_service import WorkspaceService
from tiny_hermes.tenancy.infrastructure.sql_store import SqlWorkspaceStore


class ApplicationResources:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = get_settings()
        return self._settings

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            self._session_factory = async_sessionmaker(
                self.database_engine(), expire_on_commit=False
            )
        return self._session_factory

    def database_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                self.settings.database_url, pool_pre_ping=True
            )
        return self._engine

    async def auth_service(self) -> AsyncGenerator[AuthService]:
        async with self.session_factory()() as session:
            service = AuthService(
                SqlAuthStore(session),
                self.settings.bootstrap_token,
                self.settings.session_ttl_seconds,
            )
            try:
                yield service
            except AppError as error:
                if error.code in {"invalid_bootstrap_token", "invalid_credentials"}:
                    await session.commit()
                else:
                    await session.rollback()
                raise
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    async def workspace_service(self) -> AsyncGenerator[WorkspaceService]:
        async with self.session_factory()() as session:
            try:
                yield WorkspaceService(SqlWorkspaceStore(session))
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    async def agent_catalog(self) -> AsyncGenerator[AgentCatalog]:
        async with self.session_factory()() as session:
            try:
                yield AgentCatalog(SqlAgentStore(session))
            except AuditedDenial:
                await session.commit()
                raise
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()
