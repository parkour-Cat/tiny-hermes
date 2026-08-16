import re
from collections.abc import Mapping, Sequence
from uuid import UUID

from pydantic import ValidationError

from tiny_hermes.agents.domain.models import (
    Agent,
    AgentDraft,
    AgentSpec,
    AgentVersion,
    EndpointModelPolicy,
    initial_agent_spec,
)
from tiny_hermes.agents.ports.store import AgentStore, PublishResult
from tiny_hermes.model_catalog.ports.store import ModelEndpointStore
from tiny_hermes.tenancy.domain.models import Actor, Role

WRITERS = {Role.WORKSPACE_ADMIN, Role.DEVELOPER}
READERS = {Role.WORKSPACE_ADMIN, Role.DEVELOPER, Role.VIEWER}

ALIAS_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALIAS_MAX_LENGTH = 80
NAME_MAX_LENGTH = 120


class AgentCatalogError(Exception):
    """Base class for every expected Agent Catalog refusal."""


class ForbiddenAgentAction(AgentCatalogError):
    pass


class UnknownAgent(AgentCatalogError):
    pass


class InvalidAgentAlias(AgentCatalogError):
    pass


class InvalidAgentName(AgentCatalogError):
    pass


class InvalidAgentSpec(AgentCatalogError):
    pass


class DraftRevisionConflict(AgentCatalogError):
    pass


class AgentAliasAlreadyUsed(AgentCatalogError):
    pass


class ModelEndpointUnavailable(AgentCatalogError):
    """The endpoint this draft names does not exist, or is no longer active."""


class ModelOutputLimitTooHigh(AgentCatalogError):
    """The draft asks for more output than the endpoint will produce."""


class AgentCatalog:
    """Agent publication rules.

    The catalog decides who may act and what a valid configuration is, then
    delegates one whole business transaction per operation to its store.
    """

    def __init__(self, store: AgentStore, endpoints: ModelEndpointStore | None = None) -> None:
        self._store = store
        # Optional so the in-memory adapter and the fast domain tests, which
        # know nothing about endpoints, keep working. A deterministic Agent
        # never reaches the check, and an endpoint-backed one cannot be
        # published without a catalog to check against.
        self._endpoints = endpoints

    async def create_agent(
        self, workspace_id: UUID, actor: Actor, name: str, alias: str, request_id: str
    ) -> Agent:
        platform = await self._require_role(workspace_id, actor, WRITERS)
        agent = await self._store.create_agent_with_draft(
            workspace_id,
            actor.id,
            _valid_name(name),
            _valid_alias(alias),
            initial_agent_spec(),
        )
        await self._audit(workspace_id, actor, "agent.created", agent.id, request_id, platform)
        return agent

    async def list_agents(
        self, workspace_id: UUID, actor: Actor, request_id: str
    ) -> Sequence[Agent]:
        platform = await self._require_role(workspace_id, actor, READERS)
        if platform:
            await self._audit(
                workspace_id, actor, "agent.listed", workspace_id, request_id, platform
            )
        return await self._store.list_agents(workspace_id)

    async def get_agent(
        self, workspace_id: UUID, actor: Actor, agent_id: UUID, request_id: str
    ) -> Agent:
        platform = await self._require_role(workspace_id, actor, READERS)
        agent = await self._store.get_agent(workspace_id, agent_id)
        if agent is None:
            raise UnknownAgent
        if platform:
            await self._audit(workspace_id, actor, "agent.read", agent.id, request_id, platform)
        return agent

    async def get_draft(
        self, workspace_id: UUID, actor: Actor, agent_id: UUID, request_id: str
    ) -> AgentDraft:
        platform = await self._require_role(workspace_id, actor, READERS)
        draft = await self._store.get_draft(workspace_id, agent_id)
        if draft is None:
            raise UnknownAgent
        if platform:
            await self._audit(
                workspace_id, actor, "agent.draft_read", agent_id, request_id, platform
            )
        return draft

    async def list_versions(
        self, workspace_id: UUID, actor: Actor, agent_id: UUID, request_id: str
    ) -> Sequence[AgentVersion]:
        platform = await self._require_role(workspace_id, actor, READERS)
        if await self._store.get_agent(workspace_id, agent_id) is None:
            raise UnknownAgent
        if platform:
            await self._audit(
                workspace_id, actor, "agent.versions_read", agent_id, request_id, platform
            )
        return await self._store.list_versions(workspace_id, agent_id)

    async def published_alias(
        self, workspace_id: UUID, actor: Actor, alias: str, request_id: str
    ) -> tuple[Agent, AgentVersion]:
        """The published version a Chat Completions `model` field names."""
        await self._require_role(workspace_id, actor, READERS)
        del request_id
        for agent in await self._store.list_agents(workspace_id):
            if agent.alias != alias:
                continue
            if agent.current_version_id is None:
                raise UnknownAgent
            versions = await self._store.list_versions(workspace_id, agent.id)
            for version in versions:
                if version.id == agent.current_version_id:
                    return agent, version
            raise UnknownAgent
        raise UnknownAgent

    async def replace_draft(
        self,
        workspace_id: UUID,
        actor: Actor,
        agent_id: UUID,
        expected_revision: int,
        spec_values: Mapping[str, object],
        request_id: str,
    ) -> AgentDraft:
        platform = await self._require_role(workspace_id, actor, WRITERS)
        draft = await self._store.replace_draft(
            workspace_id, agent_id, actor.id, expected_revision, _valid_spec(spec_values)
        )
        if draft is None:
            raise UnknownAgent
        await self._audit(
            workspace_id, actor, "agent.draft_replaced", agent_id, request_id, platform
        )
        return draft

    async def publish(
        self,
        workspace_id: UUID,
        actor: Actor,
        agent_id: UUID,
        expected_revision: int,
        request_id: str,
    ) -> PublishResult:
        platform = await self._require_role(workspace_id, actor, WRITERS)
        # Checked here rather than when the draft is saved: a draft is a work in
        # progress and an author is allowed to save one that is not ready. A
        # version is immutable and a Run will execute it, so this is the last
        # moment a mistake is still cheap.
        draft = await self._store.get_draft(workspace_id, agent_id)
        if draft is not None:
            await self._check_endpoint(draft.spec)
        result = await self._store.publish_draft(
            workspace_id, agent_id, actor.id, expected_revision
        )
        if result is None:
            raise UnknownAgent
        action = "agent.publish_unchanged" if result.unchanged else "agent.published"
        await self._audit(workspace_id, actor, action, result.version.id, request_id, platform)
        return result

    async def activate_version(
        self,
        workspace_id: UUID,
        actor: Actor,
        agent_id: UUID,
        version_id: UUID,
        request_id: str,
    ) -> AgentVersion:
        platform = await self._require_role(workspace_id, actor, WRITERS)
        version = await self._store.activate_version(workspace_id, agent_id, version_id)
        if version is None:
            raise UnknownAgent
        await self._audit(
            workspace_id, actor, "agent.version_activated", version.id, request_id, platform
        )
        return version

    async def _check_endpoint(self, spec: AgentSpec) -> None:
        """Refuse a version that names an endpoint it cannot actually use."""
        policy = spec.model_policy
        if not isinstance(policy, EndpointModelPolicy):
            return
        if self._endpoints is None:
            raise ModelEndpointUnavailable
        endpoint = await self._endpoints.read(policy.endpoint_id)
        if endpoint is None or not endpoint.is_selectable:
            raise ModelEndpointUnavailable
        wanted = policy.max_output_tokens
        if wanted is not None and wanted > endpoint.spec.max_output_tokens:
            # Refused rather than clamped. An author who asked for 8192 and got
            # 4096 has an Agent that behaves unlike the one they published, and
            # nothing anywhere would say so.
            raise ModelOutputLimitTooHigh

    async def _require_role(
        self, workspace_id: UUID, actor: Actor, allowed: set[Role]
    ) -> bool:
        """Return True when the actor acts with platform authority only."""
        if actor.is_service_account:
            if actor.role is None or actor.role not in allowed:
                raise ForbiddenAgentAction
            return False
        role = await self._store.role_for(workspace_id, actor.id)
        if role is not None:
            if role not in allowed:
                raise ForbiddenAgentAction
            return False
        if not actor.is_platform_admin:
            raise ForbiddenAgentAction
        return True

    async def _audit(
        self,
        workspace_id: UUID,
        actor: Actor,
        action: str,
        resource_id: UUID,
        request_id: str,
        platform: bool,
    ) -> None:
        await self._store.append_audit(
            workspace_id,
            actor.id,
            f"{action}_by_platform_admin" if platform else action,
            resource_id,
            request_id,
        )


def _valid_name(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > NAME_MAX_LENGTH:
        raise InvalidAgentName
    return normalized


def _valid_alias(value: str) -> str:
    normalized = value.strip()
    if len(normalized) > ALIAS_MAX_LENGTH or not ALIAS_PATTERN.match(normalized):
        raise InvalidAgentAlias
    return normalized


def _valid_spec(values: Mapping[str, object]) -> AgentSpec:
    try:
        return AgentSpec.model_validate(dict(values))
    except ValidationError as error:
        raise InvalidAgentSpec from error
