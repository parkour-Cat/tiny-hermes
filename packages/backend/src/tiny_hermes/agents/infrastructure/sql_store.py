from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.agents.application.service import (
    AgentAliasAlreadyUsed,
    DraftRevisionConflict,
)
from tiny_hermes.agents.domain.models import (
    Agent,
    AgentDraft,
    AgentSpec,
    AgentStatus,
    AgentVersion,
    normalize_agent_spec,
)
from tiny_hermes.agents.infrastructure.tables import AgentDraftRow, AgentRow, AgentVersionRow
from tiny_hermes.agents.ports.store import PublishResult
from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow

ALIAS_CONSTRAINT = "uq_agents_workspace_alias"


class SqlAgentStore:
    """PostgreSQL Agent Catalog adapter.

    Each method is one whole business transaction step; the surrounding request
    dependency owns the commit.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def role_for(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        value = await self._session.scalar(
            select(MembershipRow.role).where(
                MembershipRow.workspace_id == workspace_id,
                MembershipRow.user_id == user_id,
            )
        )
        return Role(value) if value else None

    async def create_agent_with_draft(
        self, workspace_id: UUID, user_id: UUID, name: str, alias: str, spec: AgentSpec
    ) -> Agent:
        document, _ = normalize_agent_spec(spec)
        row = AgentRow(
            id=uuid4(),
            workspace_id=workspace_id,
            name=name,
            alias=alias,
            status="draft",
            current_version_id=None,
            created_at=datetime.now(UTC),
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            # The memory adapter looks the alias up before inserting, which is
            # honest there and racy here: two concurrent requests would both
            # pass such a check. ``uq_agents_workspace_alias`` is the real
            # guard, so this translates it into the same failure the other
            # adapter raises rather than adding a second, weaker one.
            if ALIAS_CONSTRAINT not in str(error.orig):
                raise
            raise AgentAliasAlreadyUsed from error
        self._session.add(
            AgentDraftRow(
                agent_id=row.id,
                spec=document,
                revision=1,
                updated_by=user_id,
                updated_at=datetime.now(UTC),
            )
        )
        await self._session.flush()
        return _agent(row)

    async def replace_draft(
        self,
        workspace_id: UUID,
        agent_id: UUID,
        user_id: UUID,
        expected_revision: int,
        spec: AgentSpec,
    ) -> AgentDraft | None:
        agent = await self._lock_agent(workspace_id, agent_id)
        if agent is None:
            return None
        draft = await self._lock_draft(agent_id)
        if draft is None:
            return None
        if draft.revision != expected_revision:
            raise DraftRevisionConflict
        document, _ = normalize_agent_spec(spec)
        draft.spec = document
        draft.revision = draft.revision + 1
        draft.updated_by = user_id
        draft.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _draft(draft, spec)

    async def publish_draft(
        self,
        workspace_id: UUID,
        agent_id: UUID,
        user_id: UUID,
        expected_revision: int,
    ) -> PublishResult | None:
        agent = await self._lock_agent(workspace_id, agent_id)
        if agent is None:
            return None
        draft = await self._lock_draft(agent_id)
        if draft is None:
            return None
        if draft.revision != expected_revision:
            raise DraftRevisionConflict

        # The stored document is re-validated so a hand-edited row can never
        # become an immutable published version.
        spec = AgentSpec.model_validate(draft.spec)
        document, content_hash = normalize_agent_spec(spec)

        if agent.current_version_id is not None:
            current = await self._session.get(AgentVersionRow, agent.current_version_id)
            if current is not None and current.content_hash == content_hash:
                return PublishResult(_version(current), True)

        highest = await self._session.scalar(
            select(func.max(AgentVersionRow.version_number)).where(
                AgentVersionRow.agent_id == agent_id
            )
        )
        row = AgentVersionRow(
            id=uuid4(),
            agent_id=agent_id,
            workspace_id=workspace_id,
            version_number=(highest or 0) + 1,
            schema_version=spec.schema_version,
            spec=document,
            content_hash=content_hash,
            published_by=user_id,
            created_at=datetime.now(UTC),
        )
        self._session.add(row)
        await self._session.flush()
        agent.current_version_id = row.id
        agent.status = "published"
        await self._session.flush()
        return PublishResult(_version(row), False)

    async def activate_version(
        self, workspace_id: UUID, agent_id: UUID, version_id: UUID
    ) -> AgentVersion | None:
        agent = await self._lock_agent(workspace_id, agent_id)
        if agent is None:
            return None
        row = await self._session.scalar(
            select(AgentVersionRow).where(
                AgentVersionRow.id == version_id,
                AgentVersionRow.agent_id == agent_id,
                AgentVersionRow.workspace_id == workspace_id,
            )
        )
        if row is None:
            return None
        agent.current_version_id = row.id
        agent.status = "published"
        await self._session.flush()
        return _version(row)

    async def get_agent(self, workspace_id: UUID, agent_id: UUID) -> Agent | None:
        row = await self._session.scalar(
            select(AgentRow).where(
                AgentRow.id == agent_id, AgentRow.workspace_id == workspace_id
            )
        )
        return None if row is None else _agent(row)

    async def update_agent(
        self,
        workspace_id: UUID,
        agent_id: UUID,
        name: str | None,
        alias: str | None,
    ) -> Agent | None:
        row = await self._lock_agent(workspace_id, agent_id)
        if row is None:
            return None
        if name is not None:
            row.name = name
        if alias is not None:
            row.alias = alias
        try:
            await self._session.flush()
        except IntegrityError as error:
            if ALIAS_CONSTRAINT not in str(error.orig):
                raise
            raise AgentAliasAlreadyUsed from error
        return _agent(row)

    async def get_draft(self, workspace_id: UUID, agent_id: UUID) -> AgentDraft | None:
        if await self.get_agent(workspace_id, agent_id) is None:
            return None
        row = await self._session.get(AgentDraftRow, agent_id)
        return None if row is None else _draft(row, AgentSpec.model_validate(row.spec))

    async def list_agents(self, workspace_id: UUID) -> Sequence[Agent]:
        rows = (
            await self._session.scalars(
                select(AgentRow)
                .where(AgentRow.workspace_id == workspace_id)
                .order_by(AgentRow.created_at, AgentRow.id)
            )
        ).all()
        return [_agent(row) for row in rows]

    async def list_versions(
        self, workspace_id: UUID, agent_id: UUID
    ) -> Sequence[AgentVersion]:
        rows = (
            await self._session.scalars(
                select(AgentVersionRow)
                .where(
                    AgentVersionRow.agent_id == agent_id,
                    AgentVersionRow.workspace_id == workspace_id,
                )
                .order_by(AgentVersionRow.version_number)
            )
        ).all()
        return [_version(row) for row in rows]

    async def append_audit(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        result: str = "succeeded",
    ) -> None:
        self._session.add(
            AuditEventRow(
                workspace_id=workspace_id,
                actor_type="user",
                actor_id=actor_id,
                action=action,
                resource_type="agent",
                resource_id=resource_id,
                result=result,
                request_id=request_id,
                context={},
            )
        )

    async def _lock_agent(self, workspace_id: UUID, agent_id: UUID) -> AgentRow | None:
        return await self._session.scalar(
            select(AgentRow)
            .where(AgentRow.id == agent_id, AgentRow.workspace_id == workspace_id)
            .with_for_update()
        )

    async def _lock_draft(self, agent_id: UUID) -> AgentDraftRow | None:
        return await self._session.scalar(
            select(AgentDraftRow)
            .where(AgentDraftRow.agent_id == agent_id)
            .with_for_update()
        )


def _agent(row: AgentRow) -> Agent:
    return Agent(
        row.id,
        row.workspace_id,
        row.name,
        row.alias,
        cast(AgentStatus, row.status),
        row.current_version_id,
        row.created_at,
    )


def _draft(row: AgentDraftRow, spec: AgentSpec) -> AgentDraft:
    return AgentDraft(row.agent_id, spec, row.revision, row.updated_by, row.updated_at)


def _version(row: AgentVersionRow) -> AgentVersion:
    return AgentVersion(
        row.id,
        row.agent_id,
        row.workspace_id,
        row.version_number,
        row.schema_version,
        row.spec,
        row.content_hash,
        row.published_by,
        row.created_at,
    )
