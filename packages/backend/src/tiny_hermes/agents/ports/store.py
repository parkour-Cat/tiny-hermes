from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from tiny_hermes.agents.domain.models import Agent, AgentDraft, AgentSpec, AgentVersion
from tiny_hermes.tenancy.domain.models import Role


@dataclass(frozen=True)
class PublishResult:
    version: AgentVersion
    unchanged: bool


class AgentStore(Protocol):
    """Business-level Agent persistence.

    Every method is one atomic operation. A ``None`` result always means the
    addressed resource does not exist inside the given workspace; conflicting
    concurrent edits raise the matching application error instead.
    """

    async def role_for(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def create_agent_with_draft(
        self, workspace_id: UUID, user_id: UUID, name: str, alias: str, spec: AgentSpec
    ) -> Agent: ...

    async def replace_draft(
        self,
        workspace_id: UUID,
        agent_id: UUID,
        user_id: UUID,
        expected_revision: int,
        spec: AgentSpec,
    ) -> AgentDraft | None: ...

    async def publish_draft(
        self,
        workspace_id: UUID,
        agent_id: UUID,
        user_id: UUID,
        expected_revision: int,
    ) -> PublishResult | None: ...

    async def activate_version(
        self, workspace_id: UUID, agent_id: UUID, version_id: UUID
    ) -> AgentVersion | None: ...

    async def get_agent(self, workspace_id: UUID, agent_id: UUID) -> Agent | None: ...

    async def published_aliases(
        self, workspace_id: UUID, aliases: Sequence[str]
    ) -> frozenset[str]:
        """Which of these aliases name an Agent with a published version here.

        Published rather than merely existing: a delegation to a draft is a
        delegation to something no Run can start, and §13's children are
        independent Runs.
        """
        ...

    async def update_agent(
        self,
        workspace_id: UUID,
        agent_id: UUID,
        name: str | None,
        alias: str | None,
    ) -> Agent | None: ...

    async def get_draft(self, workspace_id: UUID, agent_id: UUID) -> AgentDraft | None: ...

    async def list_agents(self, workspace_id: UUID) -> Sequence[Agent]: ...

    async def list_versions(
        self, workspace_id: UUID, agent_id: UUID
    ) -> Sequence[AgentVersion]: ...

    async def append_audit(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        result: str = "succeeded",
        context: dict[str, object] | None = None,
    ) -> None: ...
