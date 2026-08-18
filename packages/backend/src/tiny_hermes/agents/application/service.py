import re
from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from tiny_hermes.agents.domain.models import (
    Agent,
    AgentDraft,
    AgentSpec,
    AgentVersion,
    ContextBudget,
    EndpointModelPolicy,
    PlatformCeilings,
    initial_agent_spec,
    rounds_above_ceiling,
)
from tiny_hermes.agents.ports.http_tools import HttpToolBindingReader
from tiny_hermes.agents.ports.skills import SkillBindingReader, SkillBindingView
from tiny_hermes.agents.ports.store import AgentStore, PublishResult
from tiny_hermes.model_catalog.domain.models import ModelEndpoint
from tiny_hermes.model_catalog.ports.store import ModelEndpointStore
from tiny_hermes.outbound.domain.scope import OutboundScope, parse_entry
from tiny_hermes.runs.domain.context_budget import (
    Accounting,
    BudgetFit,
    ContextWindow,
    SegmentName,
    estimate_tokens,
    fit_budget,
)
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


class ContextBudgetUnsatisfied(AgentCatalogError):
    """The segment targets do not fit the endpoint, though the minimums do.

    Carries the per-segment advice §7.4.2 asks for, and applies none of it. An
    author whose 4096-token tool schema budget were silently cut to 900 would
    have published an Agent that behaves unlike the one they wrote.
    """

    def __init__(self, fit: BudgetFit) -> None:
        super().__init__(f"{fit.asked} tokens of targets, {fit.allowance} available")
        self.fit = fit


class ContextWindowTooSmall(AgentCatalogError):
    """Not even the untrimmable minimum fits this endpoint's window.

    A different refusal from the one above because nothing an author can scale
    would help: §7.4.2 sends this case to a static publish failure, and its
    runtime twin is `paused(context_overflow)`.
    """

    def __init__(self, floor: int, allowance: int) -> None:
        super().__init__(f"{floor} tokens are kept regardless, {allowance} available")
        self.floor = floor
        self.allowance = allowance


class WorkspaceScopeReader(Protocol):
    """What this module needs from the outbound scopes, and nothing more.

    A reader, never a writer: publishing an Agent measures against what a
    workspace approved and can never change it.
    """

    async def workspace(self, workspace_id: UUID) -> OutboundScope: ...


class AgentNetworkOutsideWorkspace(AgentCatalogError):
    """Targets this Agent named that its workspace has not approved.

    All of them, because publishing four times to find four problems is how an
    author learns to stop reading the message.
    """

    def __init__(self, entries: tuple[str, ...]) -> None:
        super().__init__(f"{len(entries)} targets outside the workspace scope")
        self.entries = entries


class AgentNetworkUnavailable(AgentCatalogError):
    """An Agent asked for the network on a platform with no scopes wired in."""

    def __init__(self, entries: tuple[str, ...]) -> None:
        super().__init__("outbound scopes are not configured here")
        self.entries = entries


class SkillBindingUnavailable(AgentCatalogError):
    """A bound skill version this Agent may not run with, and why not.

    The same reasoning as `unknown tool`: a version that was withdrawn, or that
    the scan blocks, or that belongs to another workspace, is a Run that fails
    on its first round — found by whoever submitted it rather than by the
    author who could have fixed it in the editor.

    `reasons` is keyed by version id because a draft binding four skills should
    not have to publish four times to find all four problems.
    """

    def __init__(self, reasons: Mapping[UUID, str]) -> None:
        super().__init__("; ".join(f"{key}: {value}" for key, value in reasons.items()))
        self.reasons = dict(reasons)


class HttpToolBindingUnavailable(AgentCatalogError):
    """A bound HTTP tool version this Agent may not call, and why not.

    Four ways to be wrong, and the same reason as `SkillBindingUnavailable` for
    finding all four here: a version that is not visible, one that was
    withdrawn, an operation the document does not declare, or a host outside
    this Agent's own `network.allow`.

    That last one is worth stating. The host was inside the *workspace* scope
    when the tool was registered; whether this Agent may reach it is a
    different question, asked of a different layer, and §16.5 says a layer may
    narrow. An Agent that binds a tool it may not connect to would publish
    cleanly and refuse on first use.
    """

    def __init__(self, reasons: Mapping[UUID, str]) -> None:
        super().__init__("; ".join(f"{key}: {value}" for key, value in reasons.items()))
        self.reasons = dict(reasons)


class SkillBoundTwice(AgentCatalogError):
    """Two versions of the same skill in one spec.

    Refused rather than resolved: nothing in the platform would be able to say
    which of the two `skill.load` means, and picking the higher version number
    would be this platform inventing an answer the author did not give.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"{name} is bound at two versions")
        self.name = name


class SkillSummaryBudgetExceeded(AgentCatalogError):
    """The bound summaries do not fit the segment that carries them.

    Sixteen skills at 200 characters is about 3200 characters against a
    `skill_summaries` ceiling of 1536 tokens, so this check is real rather than
    ceremonial. It carries the estimate for every summary, the same rule
    `ContextBudgetUnsatisfied` set: a refusal with a number in it is one the
    author can act on.
    """

    def __init__(self, estimates: Mapping[str, int], allowance: int) -> None:
        super().__init__(f"{sum(estimates.values())} tokens of summaries, {allowance} available")
        self.estimates = dict(estimates)
        self.allowance = allowance


class RoundCeilingExceeded(AgentCatalogError):
    """The draft asks for more model rounds than this platform allows.

    Carries both numbers: an author who typed 40 and is told only "invalid"
    has no way to find out that 20 was the answer.
    """

    def __init__(self, asked: int, allowed: int) -> None:
        super().__init__(f"{asked} model calls asked, {allowed} allowed")
        self.asked = asked
        self.allowed = allowed


class AgentCatalog:
    """Agent publication rules.

    The catalog decides who may act and what a valid configuration is, then
    delegates one whole business transaction per operation to its store.
    """

    def __init__(
        self,
        store: AgentStore,
        endpoints: ModelEndpointStore | None = None,
        ceilings: PlatformCeilings | None = None,
        skills: SkillBindingReader | None = None,
        scopes: WorkspaceScopeReader | None = None,
        http_tools: HttpToolBindingReader | None = None,
    ) -> None:
        self._store = store
        # Defaulted rather than required so the domain tests, which are about
        # publication rules and not about configuration, keep reading the way
        # they did. The default is the same 20 the field used to spell out.
        self._ceilings = ceilings or PlatformCeilings()
        # Optional so the in-memory adapter and the fast domain tests, which
        # know nothing about endpoints, keep working. A deterministic Agent
        # never reaches the check, and an endpoint-backed one cannot be
        # published without a catalog to check against.
        self._endpoints = endpoints
        # Optional for the same reason, and with the same consequence: a draft
        # that asks for no network never reaches the check, and one that does
        # cannot be published without a scope reader to check against.
        self._scopes = scopes
        # Optional for the same reason, with the same consequence: a draft that
        # binds no skill never asks, and one that binds a skill without a
        # reader to check it against cannot be published.
        self._skills = skills
        # And once more, for HTTP tools.
        self._http_tools = http_tools

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

    async def update_agent(
        self,
        workspace_id: UUID,
        actor: Actor,
        agent_id: UUID,
        name: str | None,
        alias: str | None,
        request_id: str,
    ) -> Agent:
        platform = await self._require_role(workspace_id, actor, WRITERS)
        agent = await self._store.update_agent(
            workspace_id,
            agent_id,
            None if name is None else _valid_name(name),
            None if alias is None else _valid_alias(alias),
        )
        if agent is None:
            raise UnknownAgent
        await self._audit(workspace_id, actor, "agent.updated", agent.id, request_id, platform)
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

    async def get_version(
        self,
        workspace_id: UUID,
        actor: Actor,
        agent_id: UUID,
        version_id: UUID,
        request_id: str,
    ) -> AgentVersion:
        platform = await self._require_role(workspace_id, actor, READERS)
        if await self._store.get_agent(workspace_id, agent_id) is None:
            raise UnknownAgent
        for version in await self._store.list_versions(workspace_id, agent_id):
            if version.id == version_id:
                if platform:
                    await self._audit(
                        workspace_id,
                        actor,
                        "agent.version_read",
                        version.id,
                        request_id,
                        platform,
                    )
                return version
        raise UnknownAgent

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
        spec = _valid_spec(spec_values)
        # Checked on save as well as on publish, unlike the endpoint rules: the
        # ceiling is a number the author typed, and telling them at the moment
        # they typed it costs nothing.
        self._check_ceilings(spec)
        draft = await self._store.replace_draft(
            workspace_id, agent_id, actor.id, expected_revision, spec
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
            await self._check_skills(workspace_id, draft.spec)
            await self._check_network(workspace_id, draft.spec)
            # After the network check, so a host measured against
            # `network.allow` is measured against entries already known to be
            # inside the workspace's.
            await self._check_http_tools(workspace_id, draft.spec)
            # Again here, and not only on save: this draft was measured against
            # whatever the ceiling was the day it was written.
            self._check_ceilings(draft.spec)
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

    def _check_ceilings(self, spec: AgentSpec) -> None:
        asked = rounds_above_ceiling(spec, self._ceilings)
        if asked is not None:
            raise RoundCeilingExceeded(asked, self._ceilings.max_model_calls)

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
        self._check_context_budget(spec, endpoint, wanted)

    async def _check_network(self, workspace_id: UUID, spec: AgentSpec) -> None:
        """Refuse a version naming a target its workspace never approved.

        §16.5: a layer may narrow and never widen. Caught here rather than at
        the connection, because an entry that can never match is a line in a
        published version that reads like a permission and is not one — and the
        Run that discovers it fails on whatever it was actually doing.

        Every offending entry travels back, not the first: an author naming
        four targets should not have to publish four times.
        """
        if spec.network is None or not spec.network.allow:
            return
        if self._scopes is None:
            raise AgentNetworkUnavailable(tuple(spec.network.allow))
        approved = await self._scopes.workspace(workspace_id)
        outside = tuple(
            entry
            for entry in spec.network.allow
            if not any(allowed.contains(parse_entry(entry)) for allowed in approved.entries)
        )
        if outside:
            raise AgentNetworkOutsideWorkspace(outside)

    async def _check_http_tools(self, workspace_id: UUID, spec: AgentSpec) -> None:
        """Refuse a version that binds an HTTP operation it cannot actually call."""
        if not spec.http_tools:
            return
        wanted = [binding.http_tool_version_id for binding in spec.http_tools]
        if self._http_tools is None:
            raise HttpToolBindingUnavailable(
                {version_id: "no HTTP tool catalog is configured" for version_id in wanted}
            )
        found = {
            view.version_id: view
            for view in await self._http_tools.visible_versions(workspace_id, wanted)
        }
        allowed = spec.network.allow if spec.network is not None else ()
        parsed = [parse_entry(entry) for entry in allowed]
        reasons: dict[UUID, str] = {}
        for binding in spec.http_tools:
            version_id = binding.http_tool_version_id
            view = found.get(version_id)
            if view is None:
                # Not "does not exist", for the reason `_check_skills` gives.
                reasons[version_id] = "no such HTTP tool version is visible here"
                continue
            if not view.active:
                reasons[version_id] = f"{view.tool_name} was withdrawn at this version"
                continue
            missing = sorted(set(binding.operations) - set(view.operation_ids))
            if missing:
                reasons[version_id] = (
                    f"{view.tool_name} declares no operation named {', '.join(missing)}"
                )
                continue
            wanted_host = parse_entry(view.host)
            if not any(entry.contains(wanted_host) for entry in parsed):
                reasons[version_id] = (
                    f"{view.tool_name} is at {view.host}, which this Agent's "
                    "network.allow does not cover"
                )
        if reasons:
            raise HttpToolBindingUnavailable(reasons)

    async def _check_skills(self, workspace_id: UUID, spec: AgentSpec) -> None:
        """Refuse a version that binds a skill it cannot actually load.

        Four ways a binding can be wrong, and all four are found here rather
        than on the Run's first round: the version is not visible from this
        workspace, it was withdrawn, the scan blocks it, or two of them are
        versions of one skill.
        """
        if not spec.skills:
            return
        wanted = [binding.skill_version_id for binding in spec.skills]
        if self._skills is None:
            raise SkillBindingUnavailable(
                {version_id: "no skill catalog is configured" for version_id in wanted}
            )
        found = {
            view.version_id: view
            for view in await self._skills.visible_versions(workspace_id, wanted)
        }
        reasons: dict[UUID, str] = {}
        for version_id in wanted:
            view = found.get(version_id)
            if view is None:
                # Not "does not exist": from here the two are the same answer,
                # and telling them apart would say whether another workspace
                # holds this version.
                reasons[version_id] = "no such skill version is visible here"
            elif not view.active:
                reasons[version_id] = f"{view.name} was withdrawn at this version"
            elif view.blocked_by_scan:
                reasons[version_id] = f"{view.name} has a blocking scan finding"
        if reasons:
            raise SkillBindingUnavailable(reasons)
        by_skill: dict[UUID, str] = {}
        for view in (found[version_id] for version_id in wanted):
            if view.skill_id in by_skill:
                raise SkillBoundTwice(by_skill[view.skill_id])
            by_skill[view.skill_id] = view.name
        self._check_summary_budget(spec, [found[version_id] for version_id in wanted])

    def _check_summary_budget(
        self, spec: AgentSpec, views: Sequence[SkillBindingView]
    ) -> None:
        """Whether the bound summaries fit the segment that carries them.

        No tokenizer here: which endpoint will serve this Agent is a question
        `_check_endpoint` answers, and the character-based bound is the
        conservative one, so a spec that passes this check passes it on every
        endpoint. §7.4.2's ceiling for the segment, as this spec resolves it.
        """
        budget = (spec.context_budget or ContextBudget()).resolve()
        allowance = budget[SegmentName.SKILL_SUMMARIES].max_tokens
        if allowance is None:
            return
        estimates = {view.name: estimate_tokens(view.description) for view in views}
        if sum(estimates.values()) > allowance:
            raise SkillSummaryBudgetExceeded(estimates, allowance)

    def _check_context_budget(
        self, spec: AgentSpec, endpoint: ModelEndpoint, wanted: int | None
    ) -> None:
        """Whether this Agent's context budget can be served by this endpoint.

        Only for endpoint-backed Agents, and not because the stand-in is a test
        double: it declares no window, so there is no number here to check
        against and nothing to refuse.
        """
        window = ContextWindow(
            context_window=endpoint.spec.context_window,
            reserved_output_tokens=wanted or endpoint.spec.max_output_tokens,
            accounting=Accounting(endpoint.spec.context_accounting.value),
            tokenizer=endpoint.spec.tokenizer,
        )
        budget = spec.context_budget or ContextBudget()
        fit = fit_budget(window, budget.resolve())
        if not fit.floor_fits:
            raise ContextWindowTooSmall(fit.floor, fit.allowance)
        if not fit.targets_fit:
            raise ContextBudgetUnsatisfied(fit)

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
