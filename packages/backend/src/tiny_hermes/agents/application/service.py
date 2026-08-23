import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from tiny_hermes.agents.domain.delegation import asked_by, scope_of_spec
from tiny_hermes.agents.domain.examples import example_for
from tiny_hermes.agents.domain.models import (
    Agent,
    AgentDraft,
    AgentSpec,
    AgentVersion,
    ContextBudget,
    EndpointModelPolicy,
    PlatformCeilings,
    WritePolicy,
    initial_agent_spec,
    rounds_above_ceiling,
)
from tiny_hermes.agents.ports.http_tools import (
    HttpToolBindingReader,
    HttpToolBindingView,
)
from tiny_hermes.agents.ports.mcp import McpBindingReader
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
from tiny_hermes.shared.errors import AuditedDenial
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


class UnknownAgentExample(AgentCatalogError):
    """No example by that slug. A typo, or a stale console asking for one
    this deployment no longer ships."""


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

    `recorded` says a security audit row was written before this was raised
    (§23 assertion 14), so the transaction must be committed rather than
    rolled back — the same shape `ForbiddenRunAction` uses, and for the same
    reason: only the raising site knows whether it wrote one.
    """

    def __init__(self, entries: tuple[str, ...]) -> None:
        super().__init__(f"{len(entries)} targets outside the workspace scope")
        self.entries = entries


class AgentNetworkRefusalRecorded(AgentNetworkOutsideWorkspace, AuditedDenial):
    """The same refusal, after its security audit row was written.

    Both parents on purpose. The request dependency commits and re-raises for
    `AuditedDenial` only, so without that half the row is rolled back with
    everything else; the route matches on `AgentNetworkOutsideWorkspace`, so
    without that half the caller stops getting a 422 naming the entries.

    Ordering matters here rather than being incidental: the service raises
    this before the dependency's `except` runs, which is why marking the
    `AppError` the route builds later cannot work — by then the transaction
    is already gone.
    """


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


class McpBindingUnavailable(AgentCatalogError):
    """A bound MCP server version this Agent may not call, and why not.

    The same four ways to be wrong as an HTTP binding, and found here for the
    same reason: the author is the person who can fix it, and publish is the
    last moment they are still holding it.
    """

    def __init__(self, reasons: Mapping[UUID, str]) -> None:
        super().__init__("; ".join(f"{key}: {value}" for key, value in reasons.items()))
        self.reasons = dict(reasons)


class DelegationTooWide(AgentCatalogError):
    """A child was offered a permission this Agent does not itself hold.

    §13's sixth clause is that a child's scope is an intersection, so an author
    who writes a wider one has made a mistake rather than a request — and
    finding it at publish is finding it while they are still holding it. The
    faces are named, because a refusal that only said "too wide" would send
    them guessing across six.
    """

    def __init__(self, offending: Mapping[str, Mapping[str, tuple[str, ...]]]) -> None:
        super().__init__(
            "; ".join(f"{alias}: {sorted(faces)}" for alias, faces in offending.items())
        )
        self.offending = {alias: dict(faces) for alias, faces in offending.items()}


class UnknownChildAgent(AgentCatalogError):
    """A delegation names an alias this workspace has no published Agent for."""

    def __init__(self, aliases: tuple[str, ...]) -> None:
        super().__init__(", ".join(aliases))
        self.aliases = aliases


class WritePolicyNotChosen(AgentCatalogError):
    """A bound operation writes and the version did not say what happens then.

    §16.3 requires the choice at publish and refuses the version without it.
    Named per tool and per operation, because an author binding four tools
    should be told which one is missing rather than that something is.
    """

    def __init__(self, offending: Mapping[str, tuple[str, ...]]) -> None:
        listed = "; ".join(
            f"{tool}: {', '.join(names)}" for tool, names in offending.items()
        )
        super().__init__(f"no write policy chosen for {listed}")
        self.offending = dict(offending)


class PreauthorizationNotPermitted(AgentCatalogError):
    """A pre-authorization published by somebody who cannot grant one.

    §16.3 calls it "a narrow scope a workspace administrator approved", so a
    developer publishing one would be granting themselves the approval the
    section requires somebody else to give.
    """

    def __init__(self, tools: tuple[str, ...]) -> None:
        super().__init__(f"a workspace administrator publishes a pre-authorization: {tools}")
        self.tools = tools


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


class EndUserAccessGateClosed(AgentCatalogError):
    """A credential named this alias, but its own Agent never opened §5's
    platform-side gate (or does not exist / is not published in this
    workspace at all — indistinguishable from "closed" on purpose, so a
    credential cannot be used to probe which aliases exist here).

    The workspace admin's problem, not the enterprise's: they are the one who
    can open `AgentSpec.end_user_access`, so the refusal names which alias.
    """

    def __init__(self, alias: str) -> None:
        super().__init__(f"end-user access is not enabled for {alias}")
        self.alias = alias


class EndUserAccessNotAssigned(AgentCatalogError):
    """This end user's own credential never named this alias.

    The enterprise's problem, not the platform's: whatever
    `AgentSpec.end_user_access` says, an alias the credential's `agents` claim
    does not list was simply never delegated to this person, and the fix is a
    decision only their employer can make.
    """

    def __init__(self, alias: str) -> None:
        super().__init__(f"this end user was not assigned {alias}")
        self.alias = alias


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
        mcp: McpBindingReader | None = None,
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
        # And once more, for HTTP tools, and once more for MCP.
        self._http_tools = http_tools
        self._mcp = mcp

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

    async def create_example_agent(
        self,
        workspace_id: UUID,
        actor: Actor,
        slug: str,
        endpoint_id: UUID,
        request_id: str,
    ) -> tuple[Agent, PublishResult]:
        """§21's last wizard step: an Agent that is ready to run.

        Composed from `create_agent`, `replace_draft` and `publish` rather
        than writing a version directly. Every role check, every publish-time
        rule and every audit event happens exactly as it would if a person had
        done the three steps by hand — and a shortcut that inserted a
        published version would be a second way into `agent_versions` that
        skips the checks `publish` exists to run.

        Published, not left as a draft: the wizard step is "create an example
        Agent", and a draft is not something anyone can run.
        """
        example = example_for(slug)
        if example is None:
            raise UnknownAgentExample
        agent = await self.create_agent(
            workspace_id, actor, example.name, example.alias, request_id
        )
        # Read rather than assumed to be 1: what a fresh draft's revision is
        # belongs to the store, and an example that hardcoded it would break
        # quietly the day that changed — with a revision conflict an
        # administrator has no way to interpret.
        blank = await self.get_draft(workspace_id, actor, agent.id, request_id)
        draft = await self.replace_draft(
            workspace_id,
            actor,
            agent.id,
            blank.revision,
            example.spec(endpoint_id),
            request_id,
        )
        published = await self.publish(
            workspace_id, actor, agent.id, draft.revision, request_id
        )
        return agent, published

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
        found = await self._find_published_alias(workspace_id, alias)
        if found is None:
            raise UnknownAgent
        return found

    async def resolve_end_user_agent(
        self, workspace_id: UUID, alias: str, credential_agents: Sequence[str]
    ) -> tuple[Agent, AgentVersion]:
        """§5's two-gate check for one end-user request naming one alias.

        Deliberately not `published_alias`: that method authorizes a platform
        `Actor` — a workspace member or a service account — and audits the
        read accordingly. An end user is neither, and folding this in would
        mean either demanding a membership no end user has or teaching that
        method to recognize a third kind of caller it was never written for
        (the same reasoning `resolve_end_user_caller` gave for staying out of
        `resolve_workspace_caller`).

        `credential_agents` is always measured against *this* `workspace_id`
        — the one the credential's own `aud` was verified against — so an
        alias naming another workspace's Agent can never resolve here, even
        if the credential lists it. That is what closes the gap the brief
        calls out: without this scoping, a signed credential could name an
        alias that happens to exist in a workspace it was never issued for.
        """
        found = await self._find_published_alias(workspace_id, alias)
        return self._end_user_gate_check(alias, found, credential_agents)

    async def resolve_end_user_agent_by_id(
        self, workspace_id: UUID, agent_id: UUID, credential_agents: Sequence[str]
    ) -> tuple[Agent, AgentVersion]:
        """Task-9 review finding A: the same two gates `resolve_end_user_agent`
        evaluates at Session-creation time, evaluated again at Run-submission
        time — keyed by the Session's own `agent_id` rather than an alias,
        because a Run submission hands this route no alias to resolve, only
        the Session it already opened. Sharing `_end_user_gate_check` with
        `resolve_end_user_agent` (instead of a second copy of the
        enabled/listed logic) is what makes "the two cannot drift" true
        structurally rather than true only until somebody edits one and
        forgets the other.

        Calling this on every submission, not only at Session creation, is
        the actual fix: before this task, closing `AgentSpec.end_user_access`
        or letting an Agent's last published version go away had no effect
        on a Session that already held a cookie, for as long as its own TTL
        (up to 8 hours) allowed it to keep submitting — the platform's own
        data, silently stale for a working day. `credential_agents` is the
        other half of the check and is a different kind of stale on purpose:
        it is `end_user_sessions.agents`, a snapshot of the credential's own
        `agents` claim taken once at exchange time, because the credential
        itself no longer exists to ask again (design's own red line — see
        that column's docstring). That half's staleness is bounded by the
        session TTL and cannot be tightened further without asking the end
        user to re-present a credential on every message, which nothing
        about this product's channel model supports; re-running this check
        on every submission is what makes the platform half not share that
        bound for no reason.
        """
        agent = await self._store.get_agent(workspace_id, agent_id)
        found = None if agent is None else await self._published_version_of(workspace_id, agent)
        alias = agent.alias if agent is not None else str(agent_id)
        return self._end_user_gate_check(alias, found, credential_agents)

    def _end_user_gate_check(
        self,
        alias: str,
        found: tuple[Agent, AgentVersion] | None,
        credential_agents: Sequence[str],
    ) -> tuple[Agent, AgentVersion]:
        listed = alias in credential_agents
        if found is not None and listed and _end_user_access_enabled(found[1]):
            return found
        if listed:
            raise EndUserAccessGateClosed(alias)
        # Not listed, whether the gate is open or not: the enterprise never
        # delegated this alias to this end user, and which way the platform
        # switch sits would not have changed that.
        raise EndUserAccessNotAssigned(alias)

    async def _find_published_alias(
        self, workspace_id: UUID, alias: str
    ) -> tuple[Agent, AgentVersion] | None:
        for agent in await self._store.list_agents(workspace_id):
            if agent.alias != alias:
                continue
            return await self._published_version_of(workspace_id, agent)
        return None

    async def _published_version_of(
        self, workspace_id: UUID, agent: Agent
    ) -> tuple[Agent, AgentVersion] | None:
        if agent.current_version_id is None:
            return None
        for version in await self._store.list_versions(workspace_id, agent.id):
            if version.id == agent.current_version_id:
                return agent, version
        return None
        return None

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
            await self._check_network(
                workspace_id, draft.spec, actor, agent_id, request_id
            )
            # After the network check, so a host measured against
            # `network.allow` is measured against entries already known to be
            # inside the workspace's.
            await self._check_http_tools(workspace_id, actor, draft.spec)
            await self._check_mcp_tools(workspace_id, actor, draft.spec)
            await self._check_delegation(workspace_id, draft.spec)
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

    async def _check_network(
        self, workspace_id: UUID, spec: AgentSpec, actor: Actor | None = None,
        agent_id: UUID | None = None, request_id: str = ""
    ) -> None:
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
            if actor is not None and agent_id is not None:
                # §23 assertion 14: refused *and recorded*. An Agent published
                # again and again against targets its workspace never approved
                # is something somebody should be able to notice afterwards,
                # and without this row there is nothing to notice — the author
                # sees a 422 and the workspace sees nothing at all.
                await self._store.append_audit(
                    workspace_id,
                    actor.id,
                    "agent.network_refused",
                    agent_id,
                    request_id,
                    result="denied",
                    context={"entries": list(outside)},
                )
                # `AuditedDenial` is this module's existing way of saying
                # "commit before re-raising" — the agents provider already
                # branches on it. Inventing a second mechanism beside it
                # would leave two things to keep in step.
                raise AgentNetworkRefusalRecorded(outside)
            raise AgentNetworkOutsideWorkspace(outside)

    async def _check_http_tools(
        self, workspace_id: UUID, actor: Actor, spec: AgentSpec
    ) -> None:
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
        self._check_write_policies(spec, found, await self._publisher_role(workspace_id, actor))

    async def _check_delegation(self, workspace_id: UUID, spec: AgentSpec) -> None:
        """Refuse a delegation that names nobody, or offers what this Agent
        does not hold.

        Both at publish rather than at runtime. A child whose scope is empty
        because the intersection removed everything would start, do nothing and
        report a refusal — and the author would learn about it from a Run
        instead of from the moment they wrote it.
        """
        policy = spec.delegation
        if policy is None:
            return
        aliases = tuple(child.alias for child in policy.children)
        published = await self._store.published_aliases(workspace_id, aliases)
        unknown = tuple(alias for alias in aliases if alias not in published)
        if unknown:
            raise UnknownChildAgent(unknown)
        mine = scope_of_spec(spec)
        offending: dict[str, Mapping[str, tuple[str, ...]]] = {}
        for child in policy.children:
            # Only the four publish-knowable faces are compared. `files` and
            # `secrets` are runtime references — see `scope_of_spec` — so a
            # comparison here would refuse the ordinary case rather than a
            # mistake, which is why `mine` leaves both empty and
            # `missing_from` is handed a scope with both cleared.
            asked = replace(asked_by(child), files=frozenset(), secrets=frozenset())
            missing = mine.missing_from(asked)
            if missing:
                offending[child.alias] = missing
        if offending:
            raise DelegationTooWide(offending)

    async def _check_mcp_tools(
        self, workspace_id: UUID, actor: Actor, spec: AgentSpec
    ) -> None:
        """Refuse a version that binds an MCP tool it cannot actually call.

        The write policy is required for *every* MCP binding, unlike an HTTP
        one where only a bound write needs it. An MCP server does not say which
        of its tools change something — there is no `GET` to read — so the
        platform cannot tell, and §16.3's choice has to be made for all of them
        or for none.
        """
        if not spec.mcp_tools:
            return
        wanted = [binding.mcp_server_version_id for binding in spec.mcp_tools]
        if self._mcp is None:
            raise McpBindingUnavailable(
                {version_id: "no MCP catalog is configured" for version_id in wanted}
            )
        found = {
            view.version_id: view
            for view in await self._mcp.visible_versions(workspace_id, wanted)
        }
        allowed = spec.network.allow if spec.network is not None else ()
        parsed = [parse_entry(entry) for entry in allowed]
        reasons: dict[UUID, str] = {}
        missing: dict[str, tuple[str, ...]] = {}
        presumed: list[str] = []
        role = await self._publisher_role(workspace_id, actor)
        for binding in spec.mcp_tools:
            version_id = binding.mcp_server_version_id
            view = found.get(version_id)
            if view is None:
                reasons[version_id] = "no such MCP server version is visible here"
                continue
            if not view.active:
                reasons[version_id] = f"{view.server_name} was withdrawn at this version"
                continue
            unknown = sorted(set(binding.tools) - set(view.tool_names))
            if unknown:
                reasons[version_id] = (
                    f"{view.server_name} advertised no tool named {', '.join(unknown)}"
                )
                continue
            wanted_host = parse_entry(view.host)
            if not any(entry.contains(wanted_host) for entry in parsed):
                reasons[version_id] = (
                    f"{view.server_name} is at {view.host}, which this Agent's "
                    "network.allow does not cover"
                )
                continue
            if binding.write_policy is None:
                missing[view.server_name] = tuple(binding.tools)
            elif binding.write_policy is WritePolicy.PREAUTHORIZED and role not in (
                Role.WORKSPACE_ADMIN,
                None,
            ):
                presumed.append(view.server_name)
        if reasons:
            raise McpBindingUnavailable(reasons)
        if missing:
            raise WritePolicyNotChosen(missing)
        if presumed:
            raise PreauthorizationNotPermitted(tuple(presumed))

    async def _publisher_role(self, workspace_id: UUID, actor: Actor) -> Role | None:
        """The role this publication is being made with.

        A service account carries its own; a person's comes from the
        membership. `None` is the platform administrator, who has no
        membership row and whose authority `_require_role` already audited.
        """
        if actor.is_service_account:
            return actor.role
        return await self._store.role_for(workspace_id, actor.id)

    def _check_write_policies(
        self,
        spec: AgentSpec,
        found: Mapping[UUID, HttpToolBindingView],
        role: Role | None,
    ) -> None:
        """§16.3's choice, forced at publish for every bound write.

        Two refusals rather than one. A version that bound a write and chose
        nothing is refused because the section says the choice must be made;
        a version that chose `preauthorized` without an administrator
        publishing it is refused because a developer granting themselves the
        approval is not the approval the section describes.
        """
        missing: dict[str, tuple[str, ...]] = {}
        presumed: list[str] = []
        for binding in spec.http_tools:
            view = found.get(binding.http_tool_version_id)
            if view is None:  # pragma: no cover - `reasons` caught it already
                continue
            writes = tuple(
                name for name in binding.operations if name in view.write_operation_ids
            )
            if not writes:
                continue
            if binding.write_policy is None:
                missing[view.tool_name] = writes
            elif binding.write_policy is WritePolicy.PREAUTHORIZED and role not in (
                Role.WORKSPACE_ADMIN,
                None,
            ):
                # `None` is the platform administrator, who has no membership
                # row here and is audited separately by `_require_role`.
                presumed.append(view.tool_name)
        if missing:
            raise WritePolicyNotChosen(missing)
        if presumed:
            raise PreauthorizationNotPermitted(tuple(presumed))

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


def _end_user_access_enabled(version: AgentVersion) -> bool:
    """§5's platform-side gate, read off a published version's own document.

    Absent means closed (`AgentSpec.end_user_access` docstring) — the same
    default every Agent published before this task existed always had.
    """
    spec = AgentSpec.model_validate(version.spec)
    return spec.end_user_access is not None and spec.end_user_access.enabled


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

