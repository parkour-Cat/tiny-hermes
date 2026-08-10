import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_execution_seconds: int = Field(default=900, ge=1, le=900)
    max_elapsed_seconds: int = Field(default=86_400, ge=60, le=86_400)
    max_model_calls: int = Field(default=20, ge=1, le=20)
    max_tool_calls: int = Field(default=50, ge=0, le=50)
    max_derived_retries: int = Field(default=3, ge=0, le=3)


class DeterministicModelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["deterministic"] = "deterministic"
    scenario: Literal["complete", "fail_replay_safe", "continue_once"] = "complete"


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    personality: str = Field(min_length=1, max_length=8192)
    model_policy: DeterministicModelPolicy
    tools: tuple[()] = ()
    limits: AgentLimits = AgentLimits()

    @field_validator("personality")
    @classmethod
    def normalize_personality(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("personality cannot be blank")
        return normalized


def normalize_agent_spec(spec: AgentSpec) -> tuple[dict[str, object], str]:
    normalized = spec.model_dump(mode="json")
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
