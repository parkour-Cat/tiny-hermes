import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    ] = "complete"


class EndpointModelPolicy(BaseModel):
    """A real model, named by the endpoint a platform administrator approved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["openai_compatible"]
    endpoint_id: UUID
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1)


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


#: Discriminated, so a `provider` the platform does not understand is refused
#: rather than falling through to the stand-in. An Agent that answers from a
#: `match` statement while its author believes it is talking to a model is the
#: one failure mode this union exists to make impossible.
ModelPolicy = Annotated[
    DeterministicModelPolicy | EndpointModelPolicy, Field(discriminator="provider")
]


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
