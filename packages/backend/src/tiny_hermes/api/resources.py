from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tiny_hermes.agents.application.service import AgentCatalog
from tiny_hermes.agents.infrastructure.sql_store import SqlAgentStore
from tiny_hermes.artifacts.application.service import ArtifactService
from tiny_hermes.artifacts.infrastructure.sql_store import SqlArtifactStore
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.application.machine_service import MachineIdentityService
from tiny_hermes.identity.infrastructure.sql_machine_store import SqlMachineIdentityStore
from tiny_hermes.identity.infrastructure.sql_store import SqlAuthStore
from tiny_hermes.model_catalog.application.service import ModelEndpointService
from tiny_hermes.model_catalog.infrastructure.sql_store import SqlModelEndpointStore
from tiny_hermes.outbound.client import SafeOutboundClient
from tiny_hermes.runs.application.service import RunCoordination
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.redis_notifier import RedisWakeUpNotifier
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.notifier import WakeUpNotifier
from tiny_hermes.secrets.application.service import KekSettings, SecretService
from tiny_hermes.secrets.domain.envelope import optional_kek
from tiny_hermes.secrets.infrastructure.sql_store import SqlSecretStore
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.shared.config import Settings, get_settings
from tiny_hermes.shared.errors import AppError, AuditedDenial
from tiny_hermes.tenancy.application.workspace_service import WorkspaceService
from tiny_hermes.tenancy.infrastructure.sql_store import SqlWorkspaceStore


class ApplicationResources:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._notifier: WakeUpNotifier | None = None
        self._object_store: MinioObjectStore | None = None

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

    async def machine_identity_service(self) -> AsyncGenerator[MachineIdentityService]:
        async with self.session_factory()() as session:
            try:
                yield MachineIdentityService(SqlMachineIdentityStore(session))
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    def wake_up_notifier(self) -> WakeUpNotifier:
        """Notifications only; the platform is correct without them."""
        if self._notifier is None:
            url = self.settings.redis_url
            self._notifier = (
                RedisWakeUpNotifier(url) if url else NullWakeUpNotifier()
            )
        return self._notifier

    async def close(self) -> None:
        if self._notifier is not None:
            await self._notifier.close()
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

    def outbound_client(self) -> SafeOutboundClient:
        """A fresh client per call, not a shared one.

        Nothing here is hot enough to need pooling, and a client that outlives a
        request is a client whose approved ranges were read at a different time
        than they are being used.
        """
        settings = self.settings
        return SafeOutboundClient(
            approved=settings.approved_networks,
            connect_timeout=settings.outbound_connect_timeout_seconds,
            read_timeout=settings.outbound_read_timeout_seconds,
            max_redirects=settings.outbound_max_redirects,
            max_response_bytes=settings.outbound_max_response_bytes,
        )

    async def model_endpoints(self) -> AsyncGenerator[ModelEndpointService]:
        async with self.session_factory()() as session:
            try:
                yield ModelEndpointService(
                    SqlModelEndpointStore(session),
                    SqlSecretStore(session),
                    optional_kek(self.settings.tiny_hermes_kek),
                )
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    async def agent_catalog(self) -> AsyncGenerator[AgentCatalog]:
        async with self.session_factory()() as session:
            try:
                yield AgentCatalog(SqlAgentStore(session), SqlModelEndpointStore(session))
            except AuditedDenial:
                await session.commit()
                raise
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    async def run_coordination(self) -> AsyncGenerator[RunCoordination]:
        async with self.session_factory()() as session:
            try:
                yield RunCoordination(SqlRunStore(session))
            except AuditedDenial:
                await session.commit()
                raise
            except AppError as error:
                if error.audited:
                    await session.commit()
                else:
                    await session.rollback()
                raise
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    def object_store(self) -> MinioObjectStore:
        if self._object_store is None:
            self._object_store = MinioObjectStore(
                endpoint=self.settings.s3_endpoint,
                access_key=self.settings.s3_access_key,
                secret_key=self.settings.s3_secret_key,
                bucket=self.settings.s3_bucket,
            )
        return self._object_store

    async def artifact_service(self) -> AsyncGenerator[ArtifactService]:
        """Reads only: nothing here writes, so nothing here commits."""
        async with self.session_factory()() as session:
            yield ArtifactService(SqlArtifactStore(session), self.object_store())

    async def secret_service(self) -> AsyncGenerator[SecretService]:
        async with self.session_factory()() as session:
            try:
                yield SecretService(
                    SqlSecretStore(session),
                    KekSettings(
                        current=self.settings.tiny_hermes_kek,
                        current_id=self.settings.tiny_hermes_kek_id,
                        previous=self.settings.tiny_hermes_previous_kek,
                        previous_id=self.settings.tiny_hermes_previous_kek_id,
                    ),
                )
            except AppError as error:
                if error.audited:
                    await session.commit()
                else:
                    await session.rollback()
                raise
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()
