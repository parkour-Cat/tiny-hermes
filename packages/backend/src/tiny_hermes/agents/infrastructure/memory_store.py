from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from tiny_hermes.agents.application.service import (
    AgentAliasAlreadyUsed,
    DraftRevisionConflict,
)
from tiny_hermes.agents.domain.models import (
    Agent,
    AgentDraft,
    AgentSpec,
    AgentVersion,
    normalize_agent_spec,
)
from tiny_hermes.agents.ports.store import PublishResult
from tiny_hermes.tenancy.domain.models import Role


class MemoryAgentStore:
    """In-memory Agent Catalog adapter used by fast domain tests."""

    def __init__(self) -> None:
        self.roles: dict[tuple[UUID, UUID], Role] = {}
        self.agents: dict[UUID, Agent] = {}
        self.drafts: dict[UUID, AgentDraft] = {}
        self.versions: dict[UUID, AgentVersion] = {}
        self.audit_actions: list[str] = []

    async def role_for(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        return self.roles.get((workspace_id, user_id))

    async def create_agent_with_draft(
        self, workspace_id: UUID, user_id: UUID, name: str, alias: str, spec: AgentSpec
    ) -> Agent:
        taken = any(
            agent.workspace_id == workspace_id and agent.alias == alias
            for agent in self.agents.values()
        )
        if taken:
            raise AgentAliasAlreadyUsed
        now = datetime.now(UTC)
        agent = Agent(uuid4(), workspace_id, name, alias, "draft", None, now)
        self.agents[agent.id] = agent
        self.drafts[agent.id] = AgentDraft(agent.id, spec, 1, user_id, now)
        return agent

    async def replace_draft(
        self,
        workspace_id: UUID,
        agent_id: UUID,
        user_id: UUID,
        expected_revision: int,
        spec: AgentSpec,
    ) -> AgentDraft | None:
        agent = await self.get_agent(workspace_id, agent_id)
        if agent is None:
            return None
        current = self.drafts[agent_id]
        if current.revision != expected_revision:
            raise DraftRevisionConflict
        updated = AgentDraft(
            agent_id, spec, current.revision + 1, user_id, datetime.now(UTC)
        )
        self.drafts[agent_id] = updated
        return updated

    async def publish_draft(
        self,
        workspace_id: UUID,
        agent_id: UUID,
        user_id: UUID,
        expected_revision: int,
    ) -> PublishResult | None:
        agent = await self.get_agent(workspace_id, agent_id)
        if agent is None:
            return None
        draft = self.drafts[agent_id]
        if draft.revision != expected_revision:
            raise DraftRevisionConflict
        document, content_hash = normalize_agent_spec(draft.spec)
        if agent.current_version_id is not None:
            current = self.versions[agent.current_version_id]
            if current.content_hash == content_hash:
                return PublishResult(current, True)
        numbers = [
            version.version_number
            for version in self.versions.values()
            if version.agent_id == agent_id
        ]
        version = AgentVersion(
            uuid4(),
            agent_id,
            workspace_id,
            max(numbers, default=0) + 1,
            draft.spec.schema_version,
            document,
            content_hash,
            user_id,
            datetime.now(UTC),
        )
        self.versions[version.id] = version
        self._point_agent_at(agent, version.id)
        return PublishResult(version, False)

    async def activate_version(
        self, workspace_id: UUID, agent_id: UUID, version_id: UUID
    ) -> AgentVersion | None:
        agent = await self.get_agent(workspace_id, agent_id)
        if agent is None:
            return None
        version = self.versions.get(version_id)
        if version is None or version.agent_id != agent_id:
            return None
        self._point_agent_at(agent, version.id)
        return version

    async def get_agent(self, workspace_id: UUID, agent_id: UUID) -> Agent | None:
        agent = self.agents.get(agent_id)
        if agent is None or agent.workspace_id != workspace_id:
            return None
        return agent

    async def published_aliases(
        self, workspace_id: UUID, aliases: Sequence[str]
    ) -> frozenset[str]:
        wanted = set(aliases)
        return frozenset(
            agent.alias
            for agent in self.agents.values()
            if agent.workspace_id == workspace_id
            and agent.alias in wanted
            and agent.current_version_id is not None
        )

    async def update_agent(
        self,
        workspace_id: UUID,
        agent_id: UUID,
        name: str | None,
        alias: str | None,
    ) -> Agent | None:
        agent = await self.get_agent(workspace_id, agent_id)
        if agent is None:
            return None
        next_alias = agent.alias if alias is None else alias
        if alias is not None:
            taken = any(
                other.workspace_id == workspace_id
                and other.alias == next_alias
                and other.id != agent_id
                for other in self.agents.values()
            )
            if taken:
                raise AgentAliasAlreadyUsed
        updated = Agent(
            agent.id,
            agent.workspace_id,
            agent.name if name is None else name,
            next_alias,
            agent.status,
            agent.current_version_id,
            agent.created_at,
        )
        self.agents[agent.id] = updated
        return updated

    async def get_draft(self, workspace_id: UUID, agent_id: UUID) -> AgentDraft | None:
        if await self.get_agent(workspace_id, agent_id) is None:
            return None
        return self.drafts.get(agent_id)

    async def list_agents(self, workspace_id: UUID) -> Sequence[Agent]:
        return sorted(
            (agent for agent in self.agents.values() if agent.workspace_id == workspace_id),
            key=lambda agent: (agent.created_at, agent.id),
        )

    async def list_versions(
        self, workspace_id: UUID, agent_id: UUID
    ) -> Sequence[AgentVersion]:
        return sorted(
            (
                version
                for version in self.versions.values()
                if version.agent_id == agent_id and version.workspace_id == workspace_id
            ),
            key=lambda version: version.version_number,
        )

    async def append_audit(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        result: str = "succeeded",
    ) -> None:
        del workspace_id, actor_id, resource_id, request_id, result
        self.audit_actions.append(action)

    def _point_agent_at(self, agent: Agent, version_id: UUID) -> None:
        self.agents[agent.id] = Agent(
            agent.id,
            agent.workspace_id,
            agent.name,
            agent.alias,
            "published",
            version_id,
            agent.created_at,
        )
