from collections.abc import AsyncGenerator
from uuid import UUID

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tiny_hermes.agents.application.service import AgentCatalog
from tiny_hermes.agents.domain.models import PlatformCeilings
from tiny_hermes.agents.infrastructure.http_tool_bindings import (
    CatalogHttpToolBindings,
)
from tiny_hermes.agents.infrastructure.mcp_bindings import CatalogMcpBindings
from tiny_hermes.agents.infrastructure.skill_bindings import CatalogSkillBindings
from tiny_hermes.agents.infrastructure.sql_store import SqlAgentStore
from tiny_hermes.artifacts.application.service import ArtifactService
from tiny_hermes.artifacts.infrastructure.sql_store import SqlArtifactStore
from tiny_hermes.http_tools.application.service import HttpToolCatalog
from tiny_hermes.http_tools.infrastructure.sql_store import SqlHttpToolStore
from tiny_hermes.identity.application.auth_service import AuthService
from tiny_hermes.identity.application.machine_service import MachineIdentityService
from tiny_hermes.identity.infrastructure.sql_machine_store import SqlMachineIdentityStore
from tiny_hermes.identity.infrastructure.sql_store import SqlAuthStore
from tiny_hermes.mcp.application.service import McpCatalog
from tiny_hermes.mcp.infrastructure.outbound_reader import (
    OutboundCapabilityReader,
)
from tiny_hermes.mcp.infrastructure.sql_store import SqlMcpStore
from tiny_hermes.model_catalog.application.service import ModelEndpointService
from tiny_hermes.model_catalog.infrastructure.sql_store import SqlModelEndpointStore
from tiny_hermes.outbound.application.service import OutboundScopes
from tiny_hermes.outbound.client import EgressRoute, SafeOutboundClient
from tiny_hermes.outbound.infrastructure.sql_store import SqlScopeStore
from tiny_hermes.runs.application.approvals import ApprovalService
from tiny_hermes.runs.application.event_stream import EventStreamHub, Poll
from tiny_hermes.runs.application.service import RunCoordination
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.infrastructure.redis_notifier import RedisWakeUpNotifier
from tiny_hermes.runs.infrastructure.sql_approval_store import SqlApprovalStore
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.notifier import WakeUpNotifier
from tiny_hermes.secrets.application.service import KekSettings, SecretService
from tiny_hermes.secrets.domain.envelope import optional_kek
from tiny_hermes.secrets.infrastructure.sql_store import SqlSecretStore
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.shared.config import Settings, get_settings
from tiny_hermes.shared.errors import AppError, AuditedDenial
from tiny_hermes.skills.application.service import SkillCatalog
from tiny_hermes.skills.infrastructure.outbound_tarball import OutboundTarballSource
from tiny_hermes.skills.infrastructure.sql_store import SqlSkillStore
from tiny_hermes.tenancy.application.workspace_service import WorkspaceService
from tiny_hermes.tenancy.infrastructure.sql_store import SqlWorkspaceStore


class ApplicationResources:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._notifier: WakeUpNotifier | None = None
        self._object_store: MinioObjectStore | None = None
        self._event_hub: EventStreamHub | None = None

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = get_settings()
        return self._settings

    def event_stream_hub(self, poll: Poll) -> EventStreamHub:
        if self._event_hub is None:
            self._event_hub = EventStreamHub(poll)
        return self._event_hub

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            self._session_factory = async_sessionmaker(
                self.database_engine(), expire_on_commit=False
            )
        return self._session_factory

    def database_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                self.settings.database_url,
                pool_pre_ping=True,
                pool_size=self.settings.database_pool_size,
                max_overflow=self.settings.database_max_overflow,
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

    def outbound_client(self, workspace_id: UUID | None = None) -> SafeOutboundClient:
        """A fresh client per call, not a shared one.

        Nothing here is hot enough to need pooling, and a client that outlives a
        request is a client whose route and scope were read at a different time
        than they are being used.

        `workspace_id` is the layer this call asks to be measured against, and
        nothing passes one yet. A skill imported into a workspace *is* that
        workspace's outbound and will name it — once workspace scopes exist
        (§4 of the M2C-1 plan). Naming one today would close every chain,
        because an unknown id is an empty layer by design, so the parameter is
        here and unused rather than wired to a table nobody has filled.
        """
        settings = self.settings
        egress = (
            None
            if not settings.egress_proxy_url or not settings.egress_proxy_token
            else EgressRoute(
                url=settings.egress_proxy_url,
                token=settings.egress_proxy_token,
                workspace_id=workspace_id,
            )
        )
        return SafeOutboundClient(
            egress=egress,
            connect_timeout=settings.outbound_connect_timeout_seconds,
            read_timeout=settings.outbound_read_timeout_seconds,
            max_redirects=settings.outbound_max_redirects,
            max_response_bytes=settings.outbound_max_response_bytes,
        )

    async def outbound_scopes(self) -> AsyncGenerator[OutboundScopes]:
        async with self.session_factory()() as session:
            try:
                yield OutboundScopes(SqlScopeStore(session))
            except AuditedDenial:
                await session.commit()
                raise
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    async def model_endpoints(self) -> AsyncGenerator[ModelEndpointService]:
        async with self.session_factory()() as session:
            try:
                yield ModelEndpointService(
                    SqlModelEndpointStore(session),
                    SqlSecretStore(session),
                    optional_kek(self.settings.tiny_hermes_kek),
                    # Registering an endpoint approves the host it names, and
                    # disabling one takes the approval away. One session, so
                    # the endpoint row and its scope entry move together.
                    OutboundScopes(SqlScopeStore(session)),
                )
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    async def agent_catalog(self) -> AsyncGenerator[AgentCatalog]:
        async with self.session_factory()() as session:
            try:
                yield AgentCatalog(
                    SqlAgentStore(session),
                    SqlModelEndpointStore(session),
                    PlatformCeilings(
                        max_model_calls=self.settings.agent_max_model_calls
                    ),
                    CatalogSkillBindings(SqlSkillStore(session)),
                    # What this workspace approved, so a published version can
                    # never name a target its workspace has not.
                    OutboundScopes(SqlScopeStore(session)),
                    CatalogHttpToolBindings(SqlHttpToolStore(session)),
                    CatalogMcpBindings(SqlMcpStore(session)),
                )
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

    async def skill_catalog(self) -> AsyncGenerator[SkillCatalog]:
        async with self.session_factory()() as session:
            try:
                yield SkillCatalog(
                    SqlSkillStore(session),
                    OutboundTarballSource(self.outbound_client),
                )
            except AuditedDenial:
                await session.commit()
                raise
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    async def approval_service(self) -> AsyncGenerator[ApprovalService]:
        """One transaction for the decision and the Run's transition both.

        A decision that landed without the Run moving is a Run parked in
        `waiting_approval` with its question already answered.
        """
        async with self.session_factory()() as session:
            try:
                yield ApprovalService(SqlApprovalStore(session))
            except AuditedDenial:
                await session.commit()
                raise
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    async def http_tool_catalog(self) -> AsyncGenerator[HttpToolCatalog]:
        async with self.session_factory()() as session:
            try:
                yield HttpToolCatalog(
                    SqlHttpToolStore(session),
                    # Registration is refused unless the host is already inside
                    # what this workspace approved — M2C-1's first consumer.
                    OutboundScopes(SqlScopeStore(session)),
                )
            except AuditedDenial:
                await session.commit()
                raise
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    async def mcp_catalog(self) -> AsyncGenerator[McpCatalog]:
        """Registering a server reads it, so this one needs the way out.

        A deployment with no egress route therefore cannot register a server —
        which is right: a row that looks usable and answers nothing is worse
        than no row.
        """
        async with self.session_factory()() as session:
            try:
                yield McpCatalog(
                    SqlMcpStore(session),
                    OutboundCapabilityReader(
                        self.session_factory(),
                        lambda: self.outbound_client(),
                        kek=optional_kek(self.settings.tiny_hermes_kek),
                    ),
                    OutboundScopes(SqlScopeStore(session)),
                )
            except AuditedDenial:
                await session.commit()
                raise
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

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
