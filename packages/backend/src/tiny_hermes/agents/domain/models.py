import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tiny_hermes.runs.domain.context_budget import (
    DEFAULT_SEGMENTS,
    SegmentBudget,
    SegmentName,
)


class AgentLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_execution_seconds: int = Field(default=900, ge=1, le=900)
    max_elapsed_seconds: int = Field(default=86_400, ge=60, le=86_400)
    # No upper literal. This model is also what a published AgentVersion's
    # stored document parses back into, and a Run loads that document every
    # time it starts; a `le=` here would mean an administrator lowering the
    # platform ceiling stopped work that was already published under the old
    # one. The ceiling is checked where a value is *written* instead — see
    # `PlatformCeilings` and `AgentCatalog`.
    max_model_calls: int = Field(default=20, ge=1)
    max_tool_calls: int = Field(default=50, ge=0, le=50)
    max_derived_retries: int = Field(default=3, ge=0, le=3)


class DeterministicModelPolicy(BaseModel):
    """The stand-in. Not a test double: a published Agent may select it.

    It performs no network call, so an air-gapped installation can still prove
    the platform works, and every test above the provider boundary can have a
    Run whose outcome is known.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["deterministic"] = "deterministic"
    scenario: Literal[
        "complete",
        "fail_replay_safe",
        "continue_once",
        "shell_once",
        # The workspace drill's scenario: the Run input is one shell command,
        # and the model itself asserts the outcome.
        "shell_from_input",
        # Asks to be woken later, then finishes. `waiting_external` needs a
        # producer that is not a test double, the same as every other state
        # this provider can reach.
        "wait_once",
        # Loads one bound skill named by the Run input, then answers with what
        # the document said. `skill.load` is a platform tool, so this scenario
        # proves the whole progressive-loading path on a host with no sandbox.
        "skill_once",
        # The governance drill: propose one change to a bound skill and stop.
        # It ends with a `pending` row and no version, which is the whole of
        # §15.3 seen from the Run's side.
        "propose_once",
        # Calls one HTTP operation this Version bound and answers with what
        # came back. The Run input may name a different one, which is how the
        # same scenario drills a refusal as well as an answer.
        "http_once",
        # The same drill against a bound MCP tool. One scenario rather than a
        # family of them: what differs between HTTP and MCP is the boundary,
        # not the shape of "call the thing and report what came back".
        "mcp_once",
        # Proposes one memory candidate named by the Run input and answers
        # with what the workspace policy did with it. `memory.remember` is a
        # platform tool, so this scenario runs on a host with no sandbox.
        "remember_once",
        # Searches the subject's own past conversations for the Run input
        # and answers with the snippets. §14.3 without a sandbox.
        "search_once",
        # Delegates to every alias this Version bound, one instruction each,
        # then answers with what came back. `agent.delegate` is a platform
        # tool, so §13's creation path is drilled on a host with no sandbox —
        # and "two children really ran" needs a producer that is not a test
        # double, the same as every other state this provider can reach.
        "delegate_once",
    ] = "complete"


class EndpointModelPolicy(BaseModel):
    """A real model, named by the endpoint a platform administrator approved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["openai_compatible"]
    endpoint_id: UUID
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1)
    #: A different endpoint to write context-compaction summaries with.
    #: `None` (the default) means this Agent's own endpoint — the Worker
    #: falls back to it, and publish's window check has nothing to compare
    #: because the window would be measured against itself. Omitted from the
    #: normalized document when absent (`normalize_agent_spec`), so an Agent
    #: that never names one keeps the content hash it always had.
    summary_endpoint_id: UUID | None = None


class ChatCompletionsDelivery(BaseModel):
    """Inbound OpenAI-compatible delivery. Off unless an Agent opts in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    sync_timeout_seconds: int = Field(default=60, ge=1, le=60)


class StopConditions(BaseModel):
    """Ceilings the judge owns, inside the ones the budget already enforces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_rounds: int | None = Field(default=None, ge=1)


class CompletionCondition(BaseModel):
    """What "finished" means for this Agent, declared rather than inferred.

    Product design §12.2. Two of these four the platform can actually check,
    and a condition that declares neither is refused: `goal.py` answers
    `continue` for a declared goal with nothing checked, so such an Agent would
    work until the round ceiling and pause.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Paths relative to `/workspace/data`, written the way the manifest and
    #: the file tools write them — one spelling, or a check fails on a file
    #: that exists.
    expected_artifacts: tuple[str, ...] = ()
    #: One command, run in the sandbox down `shell.exec`'s path. Never on the
    #: host: reusing that path is what carries 0.1's host-fallback ban here
    #: unchanged.
    verification_command: str | None = Field(default=None, max_length=4096)
    #: Handed to the model, never machine-checked. Declared as unenforced so
    #: nobody reads it as a guarantee, and recorded so a reader can see what
    #: the Agent was told.
    constraints: str | None = Field(default=None, max_length=4096)
    stop_conditions: StopConditions = StopConditions()

    @field_validator("expected_artifacts")
    @classmethod
    def normalize_artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        from tiny_hermes.session_workspace.domain.manifest import (  # noqa: PLC0415 - cycle
            InvalidWorkspacePath,
            normalize_workspace_path,
        )

        normalized: list[str] = []
        for path in value:
            try:
                clean = normalize_workspace_path(path)
            except InvalidWorkspacePath as refused:
                raise ValueError(f"expected artifact: {refused}") from refused
            if clean in normalized:
                # Two spellings of one file would be counted twice by a check
                # that can only answer once.
                raise ValueError(f"an artifact may be expected once: {clean}")
            normalized.append(clean)
        return tuple(normalized)

    @field_validator("verification_command")
    @classmethod
    def reject_blank_command(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("verification_command cannot be blank")
        return stripped

    @model_validator(mode="after")
    def require_something_checkable(self) -> "CompletionCondition":
        if not self.expected_artifacts and self.verification_command is None:
            raise ValueError(
                "a completion condition needs something checkable: "
                "expected_artifacts or verification_command"
            )
        return self


class SegmentOverride(BaseModel):
    """One segment's budget, as an Agent author chose to change it.

    Both numbers are optional because an author who only wants more memory
    should not have to restate a target they are happy with. What is left out
    keeps the platform default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    segment: SegmentName
    target_tokens: int | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def stay_inside_the_platform_caps(self) -> "SegmentOverride":
        default = DEFAULT_SEGMENTS[self.segment]
        if default.max_tokens is None:
            # 最近历史 is given 剩余空间 rather than a number, so there is no cap
            # here to raise and no target to set: it receives whatever the
            # other segments did not use.
            raise ValueError(f"{self.segment.value} takes what is left; it has no budget to set")
        cap = self.max_tokens if self.max_tokens is not None else default.max_tokens
        if self.max_tokens is not None and self.max_tokens > default.max_tokens:
            # §7.4.2 gives the hard cap to the platform administrator and the
            # adjustment to the author, inside it.
            raise ValueError(
                f"{self.segment.value} max_tokens {self.max_tokens} is above "
                f"this platform's cap of {default.max_tokens}"
            )
        if self.target_tokens is None:
            return self
        if self.target_tokens < default.min_tokens:
            # A target under the floor is not a smaller budget, it is an
            # unreachable one: the floor is kept regardless.
            raise ValueError(
                f"{self.segment.value} target_tokens {self.target_tokens} is below "
                f"its minimum of {default.min_tokens}"
            )
        if self.target_tokens > cap:
            raise ValueError(
                f"{self.segment.value} target_tokens {self.target_tokens} is above "
                f"its max_tokens of {cap}"
            )
        return self


class ContextBudget(BaseModel):
    """Per-segment adjustments to §7.4.2's table, inside the platform's caps.

    A tuple rather than a mapping so the normalized document has one spelling
    and one order — the same reason `tools` and `expected_artifacts` are
    tuples. What is not named here is the platform default, so a budget that
    changes one segment says one thing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    segments: tuple[SegmentOverride, ...] = ()
    #: `None` means "use the platform default"
    #: (`context_budget.DEFAULT_COMPACTION_THRESHOLD`), the same reading
    #: every other unset field in this class gets. The bound here is only
    #: what a ratio can mean at all — zero, negative, more than the whole
    #: allowance. Whether it is inside `MIN_COMPACTION_THRESHOLD` /
    #: `MAX_COMPACTION_THRESHOLD` is checked at publish
    #: (`AgentCatalog._check_compaction_threshold`) instead of here — not
    #: because this module cannot import those constants (it already imports
    #: `context_budget` above), but because bounds enforcement is a
    #: publish-authority decision: an author must be able to save a draft
    #: that is out of bounds while still working on it, and only publish
    #: refuses it.
    compaction_threshold: float | None = Field(default=None, gt=0, le=1)

    @field_validator("segments")
    @classmethod
    def one_entry_per_segment(
        cls, value: tuple[SegmentOverride, ...]
    ) -> tuple[SegmentOverride, ...]:
        named = [override.segment for override in value]
        if len(set(named)) != len(named):
            raise ValueError("a segment may be adjusted once")
        return value

    def resolve(self) -> Mapping[SegmentName, SegmentBudget]:
        """The effective table: the platform's, with this Agent's changes on top."""
        resolved = dict(DEFAULT_SEGMENTS)
        for override in self.segments:
            budget = resolved[override.segment]
            resolved[override.segment] = replace(
                budget,
                target_tokens=(
                    override.target_tokens
                    if override.target_tokens is not None
                    else budget.target_tokens
                ),
                max_tokens=(
                    override.max_tokens if override.max_tokens is not None else budget.max_tokens
                ),
            )
        return resolved


#: A spec may bind at most this many skills. Not a performance number: sixteen
#: summaries is already more than the `skill_summaries` segment will hold, so
#: the arithmetic below refuses most drafts long before this does.
MAX_SKILL_BINDINGS = 16


class SkillBinding(BaseModel):
    """One skill version an Agent may load from, named by version id.

    Red line two of M2B: a binding names a version, never a skill. Binding a
    name would mean a published Agent's behaviour changes when somebody
    uploads to the catalog — the immutability an AgentVersion promises would
    stop at the edge of its own row.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_version_id: UUID


#: A spec may bind at most this many HTTP tool versions, and at most this many
#: operations across all of them. The second number is the one that matters: an
#: operation is a tool in the model's list, and a model handed two hundred tools
#: chooses worse than one handed twelve.
MAX_HTTP_TOOL_BINDINGS = 8
MAX_BOUND_OPERATIONS = 32


class WritePolicy(StrEnum):
    """What happens when a bound operation would change something out there.

    §16.3 requires an AgentVersion to choose one of exactly these three for
    every tool that could need a person's decision, and to fail publication if
    it chose none. The point of forcing the choice is that all three are
    defensible and none of them is a safe default: silently disabling would
    surprise the author, silently pre-authorizing would be the platform
    granting a permission nobody granted, and silently escalating everything
    would turn administrators into a queue.
    """

    #: The write is refused at runtime and no approval is ever asked for. What
    #: an Agent that reads an API but must never write to it declares.
    DISABLED = "disabled"
    #: A workspace administrator approved this narrow scope when the version
    #: was published, so the call runs without stopping. Only a workspace or
    #: platform administrator may publish a version that says this — otherwise
    #: it would be a developer granting themselves the approval.
    PREAUTHORIZED = "preauthorized"
    #: Each call stops and waits for a workspace or platform administrator.
    GOVERNANCE = "governance"


class HttpToolBinding(BaseModel):
    """One HTTP tool version an Agent may call, and which of its operations.

    Two decisions worth stating.

    A binding names a **version**, the same red line as `SkillBinding`. The
    document lives on somebody else's server; if a binding named the tool then
    their next export — a parameter becoming required, an operation quietly
    turning into a write — would change what a published Agent does with nobody
    here publishing anything.

    `operations` is a **subset that must be written down**. Empty is refused
    rather than read as "all of them": a document with forty operations bound
    by an author who wanted two is thirty-eight capabilities nobody chose, and
    every other default on this boundary already fails closed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    http_tool_version_id: UUID
    operations: tuple[str, ...] = Field(min_length=1)
    #: What happens when one of these operations writes. Absent is refused at
    #: publish when any bound operation is a write — §16.3 wants the choice
    #: made rather than defaulted. Absent is fine for a read-only binding,
    #: which is why the check lives at publish where the operations are known
    #: and not here where only their names are.
    write_policy: WritePolicy | None = None

    @field_validator("operations")
    @classmethod
    def reject_repeated_operations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("an operation may be bound once")
        return value


class McpToolBinding(BaseModel):
    """One MCP server version an Agent may call, and which of its tools.

    §16.2 forbids handing a model everything a server discovered, so `tools` is
    a subset that has to be written down and an empty one is refused. There is
    deliberately no field that could say "all": a field like that gets written
    as "all" on the first day, and a server that later advertises forty more
    tools would widen a published Agent with nobody publishing anything.

    The binding names a **version**, which here is a reviewed snapshot of what
    the server advertised. The snapshot fixes which names may be offered; the
    server still decides what each one takes, and §16.2's revalidation reads
    that fresh before every Run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mcp_server_version_id: UUID
    tools: tuple[str, ...] = Field(min_length=1)
    #: What happens when one of these tools writes. An MCP server does not say
    #: which of its tools change something — there is no `GET` to read — so the
    #: platform cannot tell, and the safe reading is that any of them might.
    #: Absent is therefore `disabled` at runtime, and publishing refuses a
    #: binding that chose nothing: §16.3 wants the choice made.
    write_policy: WritePolicy | None = None

    @field_validator("tools")
    @classmethod
    def reject_repeated_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("a tool may be bound once")
        return value


#: The most MCP servers one Agent may bind, and the most tools across them. The
#: second number is the one that matters, for the reason `MAX_BOUND_OPERATIONS`
#: gives: an advertised tool is a tool in the model's list.
MAX_MCP_BINDINGS = 8
MAX_BOUND_MCP_TOOLS = 32


class ChildBinding(BaseModel):
    """One child Agent this Agent may delegate to, and on what terms.

    The alias rather than an id, because that is what an author writes and what
    a model names in a call. It is resolved to a Version at publish, so a child
    that was renamed or withdrawn is caught by the person who bound it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: str = Field(min_length=1, max_length=64)
    #: The six faces, each a list of names. Empty is empty: a face nobody named
    #: grants nothing, and there is no way to write "everything the parent has"
    #: — a child should be given what it needs rather than what is available.
    tools: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    network: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    memory: tuple[Literal["memory.read_private", "memory.propose_private"], ...] = ()


class DelegationPolicy(BaseModel):
    """Who this Agent may delegate to, and how many at once.

    Its own optional document rather than a field on `AgentLimits`, and that is
    a deliberate correction to this plan's own §8: `limits` is serialized into
    every normalized spec, so a field there would put a new key in every
    published version's document and change every content hash this platform
    has written. An absent `delegation` carries no key at all, which is the
    same promise `skills`, `http_tools` and `mcp_tools` each made in turn.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    children: tuple[ChildBinding, ...] = Field(min_length=1)
    #: How many children may run at once. The shape of this Agent's work rather
    #: than an operator's decision — a batch reconciler wants five, a summary
    #: Agent wants one — which is why it lives with the binding rather than on
    #: the workspace.
    max_parallel: int = Field(default=2, ge=1, le=8)

    @field_validator("children")
    @classmethod
    def reject_repeated_aliases(
        cls, value: tuple[ChildBinding, ...]
    ) -> tuple[ChildBinding, ...]:
        if len({child.alias for child in value}) != len(value):
            raise ValueError("a child Agent may be bound once")
        return value


#: Discriminated, so a `provider` the platform does not understand is refused
#: rather than falling through to the stand-in. An Agent that answers from a
#: `match` statement while its author believes it is talking to a model is the
#: one failure mode this union exists to make impossible.
ModelPolicy = Annotated[
    DeterministicModelPolicy | EndpointModelPolicy, Field(discriminator="provider")
]


#: The most entries one Agent may name. Not arithmetic like the skill ceiling,
#: just a bound: a list nobody can read is a list nobody reviews, and an Agent
#: that needs forty hosts is an Agent whose workspace should approve a wildcard.
MAX_NETWORK_ENTRIES = 32


class AgentNetwork(BaseModel):
    """The targets this Agent asked for, checked against its workspace at publish.

    A separate model rather than a bare tuple so the document has somewhere to
    grow — §16.5's Run-level narrowing and M2E's delegation both belong beside
    `allow` rather than in a second field on the spec.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    allow: tuple[str, ...] = ()

    @field_validator("allow")
    @classmethod
    def reject_unreadable_entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Parsed here so a draft cannot be saved with a line nobody can act on.

        The same parser the platform and workspace levels use, so what an
        author may write and what a connection is measured against cannot
        drift apart.
        """
        from tiny_hermes.outbound.domain.scope import (  # noqa: PLC0415 - cycle
            ScopeEntryInvalid,
            parse_entry,
        )

        if len(value) > MAX_NETWORK_ENTRIES:
            raise ValueError(f"an Agent may name at most {MAX_NETWORK_ENTRIES} targets")
        seen: list[str] = []
        for entry in value:
            try:
                parsed = parse_entry(entry)
            except ScopeEntryInvalid as refused:
                raise ValueError(str(refused)) from refused
            if parsed.text in seen:
                raise ValueError(f"a target may be named once: {parsed.text}")
            seen.append(parsed.text)
        return tuple(seen)


class EndUserAccess(BaseModel):
    """§5's platform-side gate for the end-user entry point (design §4.5.2).

    Two independent layers decide whether an end user may reach an Agent: this
    one, an Agent author's own opt-in, and the enterprise's `agents` claim on
    the credential it signs. Both must agree — an author who never wrote this
    document has not exposed their Agent to anyone's end users, whatever an
    enterprise's credential later claims, and an enterprise that never lists
    an alias has not granted it, however wide the author left this open.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False


class AgentSpec(BaseModel):
    """A published Agent's whole configuration.

    ``schema_version`` stays 1 through the arrival of ``EndpointModelPolicy``,
    because adding a union member is a widening: every spec that validated
    before still validates, and normalizes to the same bytes with the same
    content hash. No published version is disturbed and no row is migrated. A
    version bump would be right for a narrowing or a rename; it is not right for
    accepting more.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    personality: str = Field(min_length=1, max_length=8192)
    model_policy: ModelPolicy
    #: Tools bound at publish, which is the whole of §10.2's first step: a Run
    #: cannot gain a capability while it executes, because the set was fixed
    #: when the version was made immutable.
    #:
    #: The widening from `tuple[()]` cost nothing. An unbound spec already
    #: serialized to `[]` and still does, so every published version keeps its
    #: content hash and `schema_version` stays 1 — pinned by a test rather than
    #: assumed.
    tools: tuple[str, ...] = ()
    limits: AgentLimits = AgentLimits()
    #: Omitted from the normalized document when it equals the default, so a
    #: spec that never heard of Chat Completions still hashes as it did.
    delivery: ChatCompletionsDelivery = ChatCompletionsDelivery()
    #: What the platform checks before it accepts a model's claim to be done.
    #: Absent for every version published before M2A, and for those the claim
    #: is still the whole of the answer — 0.1's behaviour, unchanged. Omitted
    #: from the normalized document when absent, so those versions keep their
    #: content hashes.
    completion: CompletionCondition | None = None
    #: Absent means §7.4.2's table as this platform configured it, which is what
    #: every version published before M2A gets. Omitted from the normalized
    #: document when absent, the third widening to make that promise and the
    #: third to have it pinned by a test.
    context_budget: ContextBudget | None = None
    #: The skill versions this Agent may load from, fixed when the version was
    #: made immutable — §10.2's first step, exactly as `tools` does it. Omitted
    #: from the normalized document when empty, the fourth widening to make
    #: that promise and the fourth to have it pinned by a test.
    #:
    #: M2E (§27.2.3) gives a sub-Agent the intersection of its parent's skills
    #: and the delegation policy. A binding is a set of version ids, so that
    #: intersection is a set intersection and nothing here has to change shape
    #: for it. Written down rather than built: this phase does not delegate.
    skills: tuple[SkillBinding, ...] = ()
    #: What this Agent may reach on the network, fixed at publish like `tools`
    #: and `skills`. Absent means nothing: an Agent that never asked for the
    #: network does not get it because its workspace has some. Omitted from the
    #: normalized document when absent, the fifth widening to leave every
    #: earlier content hash alone and the fifth to have a test say so.
    network: AgentNetwork | None = None
    #: The HTTP tool versions this Agent may call, and which operations of each
    #: — §16.2's first step for tools that did not exist when the platform was
    #: built. Fixed at publish like `tools` and `skills`. Omitted from the
    #: normalized document when empty, the sixth widening to leave every earlier
    #: content hash alone and the sixth to have a test say so.
    http_tools: tuple[HttpToolBinding, ...] = ()
    #: The MCP server versions this Agent may call, and which tools of each.
    #: §16.2's first step again, for tools whose shape somebody else's process
    #: decides. Omitted from the normalized document when empty, the seventh
    #: widening to leave every earlier content hash alone.
    mcp_tools: tuple[McpToolBinding, ...] = ()
    #: §13's delegation. Absent when this Agent delegates to nobody, and then
    #: carrying no key — the eighth widening to leave every earlier content
    #: hash exactly as it was.
    delegation: DelegationPolicy | None = None
    #: §5's platform-side gate for the end-user entry point. Absent means
    #: closed, the same default every Agent had before this key existed, so a
    #: published version's content hash is undisturbed unless its author opts
    #: in. Omitted from the normalized document when absent, the ninth
    #: widening to leave every earlier content hash exactly as it was.
    end_user_access: EndUserAccess | None = None

    @field_validator("mcp_tools")
    @classmethod
    def reject_repeated_mcp_versions(
        cls, value: tuple[McpToolBinding, ...]
    ) -> tuple[McpToolBinding, ...]:
        if len(value) > MAX_MCP_BINDINGS:
            raise ValueError(f"an Agent may bind at most {MAX_MCP_BINDINGS} MCP servers")
        named = {binding.mcp_server_version_id for binding in value}
        if len(named) != len(value):
            raise ValueError("an MCP server version may be bound only once")
        total = sum(len(binding.tools) for binding in value)
        if total > MAX_BOUND_MCP_TOOLS:
            raise ValueError(f"an Agent may bind at most {MAX_BOUND_MCP_TOOLS} MCP tools")
        return value

    @field_validator("http_tools")
    @classmethod
    def reject_repeated_http_versions(
        cls, value: tuple[HttpToolBinding, ...]
    ) -> tuple[HttpToolBinding, ...]:
        """What a draft can settle without asking the catalog anything.

        Whether an operation exists, whether the version is still bindable and
        whether its host is inside this Agent's network is checked at publish,
        where there is a store to ask.
        """
        if len(value) > MAX_HTTP_TOOL_BINDINGS:
            raise ValueError(
                f"an Agent may bind at most {MAX_HTTP_TOOL_BINDINGS} HTTP tools"
            )
        named = {binding.http_tool_version_id for binding in value}
        if len(named) != len(value):
            raise ValueError("an HTTP tool version may be bound only once")
        total = sum(len(binding.operations) for binding in value)
        if total > MAX_BOUND_OPERATIONS:
            raise ValueError(
                f"an Agent may bind at most {MAX_BOUND_OPERATIONS} operations"
            )
        return value

    @field_validator("skills")
    @classmethod
    def reject_repeated_versions(
        cls, value: tuple[SkillBinding, ...]
    ) -> tuple[SkillBinding, ...]:
        """One version, once. Two versions of one skill is refused at publish,
        where the store can say which skill a version belongs to; this catches
        the case a draft can settle on its own."""
        if len(value) > MAX_SKILL_BINDINGS:
            raise ValueError(f"an Agent may bind at most {MAX_SKILL_BINDINGS} skills")
        seen = {binding.skill_version_id for binding in value}
        if len(seen) != len(value):
            raise ValueError("a skill version may be bound only once")
        return value

    @field_validator("tools")
    @classmethod
    def reject_unknown_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Refused at publish rather than at the first call.

        An AgentVersion naming a tool nobody implemented would be a Run that
        fails on its first model round, discovered by whoever submitted it
        instead of by the person who wrote it.
        """
        from tiny_hermes.tools.domain.registry import (  # noqa: PLC0415 - cycle
            IMPLEMENTED_TOOLS,
        )

        if len(set(value)) != len(value):
            raise ValueError("a tool may be bound once")
        for name in value:
            if name not in IMPLEMENTED_TOOLS:
                raise ValueError(f"unknown tool: {name}")
        return value

    @model_validator(mode="after")
    def require_the_means_to_check_completion(self) -> "AgentSpec":
        """Refused at publish, for the same reason an unknown tool is.

        Both of these describe a check the platform could never run, and a
        check that never runs is a Run that works until the round ceiling and
        pauses — a runtime mystery for whoever submitted it, caused by
        something its author could have been told while writing it.
        """
        completion = self.completion
        if completion is None:
            return self
        if completion.verification_command is not None and "shell.exec" not in self.tools:
            raise ValueError(
                "a verification_command runs down shell.exec's path; bind shell.exec"
            )
        if completion.expected_artifacts and not self.tools:
            # No bound tool means no sandbox for the slice at all
            # (`worker.py:249`), so nothing could write the artifact.
            raise ValueError(
                "an expected artifact needs a tool that can produce it; bind one"
            )
        declared = completion.stop_conditions.max_rounds
        if declared is not None and declared > self.limits.max_model_calls:
            # The budget would stop the Run first, while the Agent's author
            # reads the declared ceiling as the one in force.
            raise ValueError("stop_conditions.max_rounds exceeds limits.max_model_calls")
        return self

    @field_validator("personality")
    @classmethod
    def normalize_personality(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("personality cannot be blank")
        return normalized


@dataclass(frozen=True)
class PlatformCeilings:
    """What a platform administrator lets an Agent author ask for.

    Product design §12.3 gives administrators the default and the maximum for
    each safety valve. Only the maximum is here: the default stays on
    `AgentLimits`, because the default is what an omitted `limits` normalizes
    to, and a default that moved with configuration would give the same
    submitted spec a different content hash on two installations.

    Read at the moment a value is written, never at the moment one is read
    back — see the comment on `AgentLimits.max_model_calls`.
    """

    max_model_calls: int = 20


def rounds_above_ceiling(spec: AgentSpec, ceilings: PlatformCeilings) -> int | None:
    """The asked-for round count when it is above the ceiling, else `None`."""
    asked = spec.limits.max_model_calls
    return asked if asked > ceilings.max_model_calls else None


def normalize_agent_spec(spec: AgentSpec) -> tuple[dict[str, object], str]:
    normalized = spec.model_dump(mode="json")
    default_delivery = ChatCompletionsDelivery().model_dump(mode="json")
    if normalized.get("delivery") == default_delivery:
        del normalized["delivery"]
    if normalized.get("completion") is None:
        # Not "equals the default": there is no default condition. A spec that
        # declares none carries no key at all, which is what keeps every
        # version published before M2A hashing exactly as it did.
        normalized.pop("completion", None)
    if normalized.get("context_budget") is None:
        # Same reasoning again: there is no default budget *document*, only a
        # platform table an absent budget means. A spec that declares none
        # carries no key, and hashes as it did before this field existed.
        normalized.pop("context_budget", None)
    budget = normalized.get("context_budget")
    # Same promise one field down, same reasoning as `summary_endpoint_id`
    # below: `compaction_threshold` did not exist when `context_budget` did,
    # so an Agent that names no ratio has to carry no key at all — otherwise
    # every version published with a `segments` override, before this field
    # existed, would hash differently the moment it was added.
    if isinstance(budget, dict):
        budget_document = cast(dict[str, object], budget)
        if budget_document.get("compaction_threshold") is None:
            budget_document.pop("compaction_threshold", None)
    if normalized.get("network") is None:
        # Same reasoning as `completion` and `context_budget`: there is no
        # default network *document*, only the absence of one, and a spec that
        # declares none must carry no key so versions published before M2C
        # hash exactly as they did.
        normalized.pop("network", None)
    if normalized.get("delegation") is None:
        # The same promise one more time, and the reason it matters most here:
        # `AgentLimits` would have been the natural home for a parallel ceiling
        # and is serialized into every spec, so putting it there would have
        # rewritten every hash. This key is absent unless an author wrote one.
        normalized.pop("delegation", None)
    if normalized.get("end_user_access") is None:
        # The ninth widening, same promise: an Agent that never opted into the
        # end-user entry point carries no key for it.
        normalized.pop("end_user_access", None)
    if not normalized.get("mcp_tools"):
        # Same promise as `http_tools`, one field later.
        normalized.pop("mcp_tools", None)
    if not normalized.get("http_tools"):
        # Same promise as `skills`, one field later: the key was not there
        # before M2C, so an Agent that binds no HTTP tool must carry no key.
        normalized.pop("http_tools", None)
    if not normalized.get("skills"):
        # `tools` could keep its empty list because the key was there from the
        # first published version. This one was not, so an empty binding set
        # has to carry no key at all to leave those hashes alone.
        normalized.pop("skills", None)
    policy = normalized.get("model_policy")
    # Unlike `temperature` and `max_output_tokens` beside it, which have
    # always serialized as an explicit `null` when unset — this key did not
    # exist when those did, so an Agent that names no summary endpoint has to
    # carry no key at all, the same promise every widening above already
    # makes for a field of its own.
    if isinstance(policy, dict):
        policy_document = cast(dict[str, object], policy)
        if policy_document.get("summary_endpoint_id") is None:
            policy_document.pop("summary_endpoint_id", None)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return normalized, hashlib.sha256(encoded).hexdigest()


def initial_agent_spec() -> AgentSpec:
    return AgentSpec(
        personality="Describe this agent before publishing.",
        model_policy=DeterministicModelPolicy(),
    )


AgentStatus = Literal["draft", "published"]


@dataclass(frozen=True)
class Agent:
    id: UUID
    workspace_id: UUID
    name: str
    alias: str
    status: AgentStatus
    current_version_id: UUID | None
    created_at: datetime


@dataclass(frozen=True)
class AgentDraft:
    agent_id: UUID
    spec: AgentSpec
    revision: int
    updated_by: UUID
    updated_at: datetime


@dataclass(frozen=True)
class AgentVersion:
    id: UUID
    agent_id: UUID
    workspace_id: UUID
    version_number: int
    schema_version: int
    spec: dict[str, object]
    content_hash: str
    published_by: UUID
    created_at: datetime
