# M1 Run Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver phase 2A: immutable Agent publication plus PostgreSQL-backed Session and Run rules, including FIFO ordering, idempotent creation, authoritative state control, contiguous events, lease claiming, head repair, and shared-budget safe retries.

**Architecture:** Add deep Agent Catalog and Run Coordination modules to the existing Python release unit. FastAPI routes call application interfaces; PostgreSQL adapters own whole business transactions rather than exposing table CRUD. The pure Run state machine is the only code that chooses state changes, and all concurrency truth remains in PostgreSQL.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2 async, PostgreSQL 17, Alembic, pytest, pytest-asyncio, httpx, React baseline checks, Docker Compose

---

## 1. Fixed scope and working rules

- Work only in `.worktrees/codex-m1-run-foundation` on branch `codex/m1-run-foundation`.
- Treat the product design v2.4 and `docs/superpowers/specs/2026-08-10-m1-run-foundation-design.md` as authoritative.
- Use `uv run --no-sync` after the initial locked install and `corepack pnpm` for Node commands.
- Write and observe a failing test before each production behavior.
- Do not create Worker, Scheduler, Redis wake-up, SSE, model, sandbox, or phase-2 UI placeholder processes.
- Every database integration command targets only `tiny_hermes_test`.
- Commit after each task only when its focused checks pass.

## 2. File map

```text
CONTEXT.md                                      # already committed domain language
packages/backend/src/tiny_hermes/
├─ agents/
│  ├─ domain/models.py                         # Agent Spec, Draft, Version values
│  ├─ application/service.py                   # Agent Catalog interface
│  ├─ ports/store.py                            # high-level persistence seam
│  ├─ infrastructure/{tables,memory_store,sql_store}.py
│  └─ presentation/routes.py                   # management HTTP routes
├─ runs/
│  ├─ domain/{models,state_machine}.py          # Run language and all state decisions
│  ├─ application/service.py                   # Run Coordination interface
│  ├─ ports/store.py                            # transaction-level commands/results
│  ├─ infrastructure/{tables,sql_store}.py
│  └─ presentation/routes.py                   # Session and Runs HTTP routes
├─ identity/presentation/dependencies.py        # shared cookie/CSRF user resolution
└─ api/{app,resources}.py                       # module assembly and transactions
migrations/versions/20260810_0002_agent_runs.py
packages/backend/tests/
├─ unit/agents/test_agent_models.py
├─ unit/agents/test_agent_service.py
├─ unit/runs/test_state_machine.py
├─ unit/runs/test_run_actions.py
├─ integration/agents/test_agent_api.py
├─ integration/runs/test_run_creation.py
├─ integration/runs/test_run_control.py
├─ integration/runs/test_run_coordination.py
├─ integration/runs/test_run_retry.py
└─ integration/test_agent_run_migration.py
```

### Task 1: Agent Version schema and normalization

**Files:**
- Create: `packages/backend/src/tiny_hermes/agents/__init__.py`
- Create: `packages/backend/src/tiny_hermes/agents/domain/__init__.py`
- Create: `packages/backend/src/tiny_hermes/agents/domain/models.py`
- Create: `packages/backend/tests/unit/agents/test_agent_models.py`

- [ ] **Step 1: Write failing normalization tests**

Create `packages/backend/tests/unit/agents/test_agent_models.py` with these behaviors:

```python
import pytest
from pydantic import ValidationError

from tiny_hermes.agents.domain.models import AgentSpec, normalize_agent_spec


def valid_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "personality": "You are concise.",
        "model_policy": {"provider": "deterministic", "scenario": "complete"},
        "tools": [],
        "limits": {
            "max_execution_seconds": 900,
            "max_elapsed_seconds": 86400,
            "max_model_calls": 20,
            "max_tool_calls": 50,
            "max_derived_retries": 3,
        },
    }


def test_agent_spec_has_stable_normalized_document_and_hash() -> None:
    first = AgentSpec.model_validate(valid_spec())
    reordered = AgentSpec.model_validate(dict(reversed(list(valid_spec().items()))))

    first_json, first_hash = normalize_agent_spec(first)
    second_json, second_hash = normalize_agent_spec(reordered)

    assert first_json == second_json
    assert first_hash == second_hash
    assert len(first_hash) == 64


def test_phase_two_rejects_tools_and_non_deterministic_provider() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**valid_spec(), "tools": ["file.read"]})
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {
                **valid_spec(),
                "model_policy": {"provider": "openai_compatible", "scenario": "complete"},
            }
        )


def test_agent_spec_rejects_unknown_fields_and_unsafe_limits() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**valid_spec(), "unknown": True})
    values = valid_spec()
    values["limits"] = {
        "max_execution_seconds": 901,
        "max_elapsed_seconds": 86400,
        "max_model_calls": 20,
        "max_tool_calls": 50,
        "max_derived_retries": 3,
    }
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(values)
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run:

```powershell
$env:UV_CACHE_DIR = ".uv-cache"
uv run --no-sync pytest packages/backend/tests/unit/agents/test_agent_models.py -v
```

Expected: collection fails because `tiny_hermes.agents.domain.models` does not exist.

- [ ] **Step 3: Implement the immutable schema and normalizer**

Create `models.py` with the exact public values below. Keep timestamps and database rows out of this file.

```python
import hashlib
import json
from typing import Literal

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
```

`initial_agent_spec()` is the one server-owned starting Draft used when `POST /agents`
contains only name and alias. It starts at revision 1. The first `PUT /draft` compares
revision 1 and replaces it with the developer's validated configuration at revision 2;
the placeholder is never copied into a published version unless the developer explicitly
publishes it.

- [ ] **Step 4: Verify green plus static checks**

Run:

```powershell
uv run --no-sync pytest packages/backend/tests/unit/agents/test_agent_models.py -v
uv run --no-sync ruff check packages/backend/src/tiny_hermes/agents packages/backend/tests/unit/agents
uv run --no-sync pyright
```

Expected: 3 tests pass and both static checks exit 0.

- [ ] **Step 5: Commit the schema**

```powershell
git add packages/backend/src/tiny_hermes/agents packages/backend/tests/unit/agents
git commit -m "feat: define immutable agent version schema"
```

### Task 2: Agent Catalog rules with two adapters

**Files:**
- Create: `packages/backend/src/tiny_hermes/agents/application/__init__.py`
- Create: `packages/backend/src/tiny_hermes/agents/application/service.py`
- Create: `packages/backend/src/tiny_hermes/agents/ports/__init__.py`
- Create: `packages/backend/src/tiny_hermes/agents/ports/store.py`
- Create: `packages/backend/src/tiny_hermes/agents/infrastructure/__init__.py`
- Create: `packages/backend/src/tiny_hermes/agents/infrastructure/memory_store.py`
- Create: `packages/backend/tests/unit/agents/test_agent_service.py`

- [ ] **Step 1: Write failing publication and permission tests**

Use the existing `Actor` and `Role` values. The tests must cover:

```python
from uuid import uuid4

import pytest

from tiny_hermes.agents.application.service import (
    AgentCatalog,
    DraftRevisionConflict,
    ForbiddenAgentAction,
)
from tiny_hermes.agents.infrastructure.memory_store import MemoryAgentStore
from tiny_hermes.tenancy.domain.models import Actor, Role

from .test_agent_models import valid_spec


async def test_publish_is_immutable_and_unchanged_publish_reuses_current_version() -> None:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.DEVELOPER
    catalog = AgentCatalog(store)

    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
    draft = await catalog.replace_draft(
        workspace_id, actor, agent.id, 1, valid_spec(), "req-2"
    )
    first = await catalog.publish(
        workspace_id, actor, agent.id, draft.revision, "req-3"
    )
    repeated = await catalog.publish(
        workspace_id, actor, agent.id, draft.revision, "req-4"
    )

    assert first.id == repeated.id
    assert first.version_number == 1
    assert len(store.versions) == 1


async def test_stale_draft_revision_is_rejected_without_overwrite() -> None:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.WORKSPACE_ADMIN
    catalog = AgentCatalog(store)
    agent = await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")

    await catalog.replace_draft(workspace_id, actor, agent.id, 1, valid_spec(), "req-2")
    with pytest.raises(DraftRevisionConflict):
        await catalog.replace_draft(
            workspace_id, actor, agent.id, 1, valid_spec(), "req-3"
        )


async def test_viewer_cannot_modify_or_publish_agent() -> None:
    workspace_id = uuid4()
    actor = Actor(uuid4(), False)
    store = MemoryAgentStore()
    store.roles[(workspace_id, actor.id)] = Role.VIEWER
    catalog = AgentCatalog(store)

    with pytest.raises(ForbiddenAgentAction):
        await catalog.create_agent(workspace_id, actor, "Analyst", "analyst", "req-1")
```

- [ ] **Step 2: Verify red**

Run the file and confirm imports fail because the application and memory adapter are absent.

- [ ] **Step 3: Define domain records and the store interface**

Append immutable `Agent`, `AgentDraft`, and `AgentVersion` dataclasses to `domain/models.py`. Define `AgentStore` with business-level methods, not generic `add()` or `save()` calls:

```python
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
    ) -> None: ...
```

- [ ] **Step 4: Implement `AgentCatalog` and the memory adapter**

`AgentCatalog` performs role checks and input parsing, then calls one store operation per atomic behavior. Use these exact permissions:

```python
WRITERS = {Role.WORKSPACE_ADMIN, Role.DEVELOPER}
READERS = {Role.WORKSPACE_ADMIN, Role.DEVELOPER, Role.VIEWER}
```

Platform administrators are allowed even without a membership and every successful cross-workspace action is audited. Alias normalization is lowercase ASCII letters, numbers, and hyphens; reject values outside `^[a-z0-9]+(?:-[a-z0-9]+)*$` or longer than 80 characters.

The memory adapter uses dictionaries keyed by workspace and resource IDs, increments Draft revisions by one, compares normalized hashes during publication, allocates `version_number=max(existing)+1`, and stores immutable version objects. It must implement every method in `AgentStore`; no test reaches into service internals other than arranging `roles` and asserting stored versions.

`AgentCatalog.create_agent` passes `initial_agent_spec()` to the store. Publishing changes
the Agent status from `draft` to `published`; unchanged publishing and rollback keep it
`published`. Disabling an Agent is outside phase 2A and gets no route or placeholder.

- [ ] **Step 5: Verify green and commit**

Run focused tests, all unit tests, Ruff, and Pyright. Commit:

```powershell
git add packages/backend/src/tiny_hermes/agents packages/backend/tests/unit/agents
git commit -m "feat: add agent catalog rules"
```

### Task 3: Authoritative Run state machine

**Files:**
- Create: `packages/backend/src/tiny_hermes/runs/__init__.py`
- Create: `packages/backend/src/tiny_hermes/runs/domain/__init__.py`
- Create: `packages/backend/src/tiny_hermes/runs/domain/models.py`
- Create: `packages/backend/src/tiny_hermes/runs/domain/state_machine.py`
- Create: `packages/backend/tests/unit/runs/test_state_machine.py`
- Create: `packages/backend/tests/unit/runs/test_run_actions.py`

- [ ] **Step 1: Write a table-driven failing state-matrix test**

Define `RunState`, `PauseReason`, `RunSignal`, `RunStateView`, and `StateDecision` as the desired interface. The test must enumerate every product transition:

```python
from datetime import UTC, datetime, timedelta

import pytest

from tiny_hermes.runs.domain.models import PauseReason, RunSignal, RunState, RunStateView
from tiny_hermes.runs.domain.state_machine import InvalidStateTransition, RunStateMachine


ALLOWED = {
    (RunState.QUEUED, RunSignal.LEASE_ACQUIRED): RunState.RUNNING,
    (RunState.QUEUED, RunSignal.PAUSE_REQUESTED): RunState.PAUSED,
    (RunState.QUEUED, RunSignal.CANCEL_REQUESTED): RunState.CANCELLED,
    (RunState.RUNNING, RunSignal.APPROVAL_REQUESTED): RunState.WAITING_APPROVAL,
    (RunState.RUNNING, RunSignal.EXTERNAL_WAIT_STARTED): RunState.WAITING_EXTERNAL,
    (RunState.RUNNING, RunSignal.SLICE_ENDED): RunState.QUEUED,
    (RunState.RUNNING, RunSignal.SAFE_PAUSE_REACHED): RunState.PAUSED,
    (RunState.RUNNING, RunSignal.SAFE_CANCEL_STARTED): RunState.CANCELLING,
    (RunState.RUNNING, RunSignal.COMPLETED): RunState.COMPLETED,
    (RunState.RUNNING, RunSignal.FAILED): RunState.FAILED,
    (RunState.RUNNING, RunSignal.INTERRUPTED): RunState.INTERRUPTED,
    (RunState.WAITING_APPROVAL, RunSignal.APPROVAL_APPROVED): RunState.QUEUED,
    (RunState.WAITING_APPROVAL, RunSignal.APPROVAL_PAUSED): RunState.PAUSED,
    (RunState.WAITING_APPROVAL, RunSignal.CANCEL_REQUESTED): RunState.CANCELLED,
    (RunState.WAITING_EXTERNAL, RunSignal.EXTERNAL_READY): RunState.QUEUED,
    (RunState.WAITING_EXTERNAL, RunSignal.EXTERNAL_PAUSED): RunState.PAUSED,
    (RunState.WAITING_EXTERNAL, RunSignal.CANCEL_REQUESTED): RunState.CANCELLED,
    (RunState.PAUSED, RunSignal.RESUME_REQUESTED): RunState.QUEUED,
    (RunState.PAUSED, RunSignal.CANCEL_REQUESTED): RunState.CANCELLED,
    (RunState.CANCELLING, RunSignal.SAFE_CANCEL_FINISHED): RunState.CANCELLED,
    (RunState.CANCELLING, RunSignal.INTERRUPTED): RunState.INTERRUPTED,
    (RunState.INTERRUPTED, RunSignal.RECOVERY_APPROVED): RunState.QUEUED,
    (RunState.INTERRUPTED, RunSignal.RECOVERY_FAILED): RunState.FAILED,
    (RunState.INTERRUPTED, RunSignal.CANCEL_REQUESTED): RunState.CANCELLED,
}


@pytest.mark.parametrize(("current", "signal", "expected"), [(*key, value) for key, value in ALLOWED.items()])
def test_authoritative_matrix_allows_only_documented_transitions(
    current: RunState, signal: RunSignal, expected: RunState
) -> None:
    machine = RunStateMachine()
    view = RunStateView(state=current)
    if signal is RunSignal.SAFE_PAUSE_REACHED:
        decision = machine.decide(view, signal, pause_reason=PauseReason.LIMIT)
    elif signal is RunSignal.APPROVAL_PAUSED:
        decision = machine.decide(
            view, signal, pause_reason=PauseReason.APPROVAL_EXPIRED
        )
    elif signal is RunSignal.EXTERNAL_PAUSED:
        decision = machine.decide(
            view, signal, pause_reason=PauseReason.EXTERNAL_TIMEOUT
        )
    elif signal is RunSignal.APPROVAL_REQUESTED:
        decision = machine.decide(view, signal, wait_kind="governance_approval")
    elif signal is RunSignal.EXTERNAL_WAIT_STARTED:
        decision = machine.decide(
            view,
            signal,
            wait_deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    else:
        decision = machine.decide(view, signal)
    assert decision.state == expected


def test_unlisted_transition_is_rejected() -> None:
    with pytest.raises(InvalidStateTransition):
        RunStateMachine().decide(
            RunStateView(state=RunState.COMPLETED), RunSignal.RESUME_REQUESTED
        )
```

- [ ] **Step 2: Add control-request and available-action failures**

Cover the non-transition request behavior:

```python
def test_running_pause_sets_request_without_claiming_paused() -> None:
    decision = RunStateMachine().decide(
        RunStateView(state=RunState.RUNNING), RunSignal.PAUSE_REQUESTED
    )
    assert decision.state == RunState.RUNNING
    assert decision.set_pause_requested is True


def test_running_cancel_sets_request_without_claiming_cancelled() -> None:
    decision = RunStateMachine().decide(
        RunStateView(state=RunState.RUNNING), RunSignal.CANCEL_REQUESTED
    )
    assert decision.state == RunState.RUNNING
    assert decision.set_cancel_requested is True


def test_budget_exhaustion_removes_resume_action() -> None:
    view = RunStateView(
        state=RunState.PAUSED,
        pause_reason=PauseReason.LIMIT,
        budget_allows_execution=False,
    )
    assert RunStateMachine().available_actions(view, can_control=True, can_retry=False) == (
        "cancel",
    )
```

- [ ] **Step 3: Implement pure values and the state machine**

Use `StrEnum` for persisted values and frozen dataclasses for views and decisions. The state machine contains the transition dictionary shown in the test. Handle `RUNNING + PAUSE_REQUESTED` and `RUNNING + CANCEL_REQUESTED` before dictionary lookup and keep the state unchanged while setting the request flag. The method signature makes transition metadata explicit instead of hiding it in a mutable view:

```python
def decide(
    self,
    view: RunStateView,
    signal: RunSignal,
    *,
    pause_reason: PauseReason | None = None,
    wait_kind: str | None = None,
    wait_deadline_at: datetime | None = None,
) -> StateDecision: ...
```

`QUEUED + PAUSE_REQUESTED` always uses `manual`. Signals that enter another paused state
must supply a reason valid for that signal: approval may use `approval_expired`,
`approval_rejected`, `approval_unavailable`, or `manual`; external wait may use
`external_timeout` or `manual`; `SAFE_PAUSE_REACHED` accepts `manual`, `limit`,
`context_overflow`, `tool_budget_exceeded`, `compat_timeout`, `operator`, or `system`.
Signals that enter waiting states must carry `wait_kind` or
`wait_deadline_at`; otherwise raise `InvalidStateMetadata`. Resume and lease acquisition
fail with `RunLimitReached` when `budget_allows_execution` is false. Terminal states expose
no actions. Failed exposes `retry` only when `can_retry` is true.

- [ ] **Step 4: Verify every state/signal pair**

Add a completeness test that iterates all enum pairs and asserts each pair is either in `ALLOWED`, one of the two running request-only cases, or raises `InvalidStateTransition`. Run both Run unit files, all unit tests, Ruff, and Pyright.

- [ ] **Step 5: Commit**

```powershell
git add packages/backend/src/tiny_hermes/runs packages/backend/tests/unit/runs
git commit -m "feat: define authoritative run state machine"
```

### Task 4: Add the phase-2A PostgreSQL schema

**Files:**
- Create: `packages/backend/src/tiny_hermes/agents/infrastructure/tables.py`
- Create: `packages/backend/src/tiny_hermes/runs/infrastructure/__init__.py`
- Create: `packages/backend/src/tiny_hermes/runs/infrastructure/tables.py`
- Create: `migrations/versions/20260810_0002_agent_runs.py`
- Modify: `migrations/env.py`
- Create: `packages/backend/tests/integration/test_agent_run_migration.py`

- [ ] **Step 1: Write the failing migration test**

Assert these ten tables exist after upgrading to head:

```python
EXPECTED = {
    "agents",
    "agent_drafts",
    "agent_versions",
    "sessions",
    "session_messages",
    "runs",
    "run_budget_scopes",
    "run_events",
    "worker_leases",
    "idempotency_records",
}
```

Also inspect and assert:

- unique `(workspace_id, alias)` on Agents;
- unique `(agent_id, version_number)` on Agent Versions;
- unique `(session_id, session_sequence)` on Runs;
- unique `(run_id, sequence)` on Run Events;
- unique idempotency scope columns;
- `runs.state_version`, `runs.next_event_sequence`, `runs.retry_of_run_id`, and `runs.budget_root_run_id` are non-null or nullable exactly as the design states;
- downgrade to `20260810_0001` removes only the ten new tables and preserves phase-one tables.

Run against a database still at migration `20260810_0001`; expect the table assertion to fail.

- [ ] **Step 2: Declare SQLAlchemy rows**

Use `UUID`, timezone timestamps, JSON, and explicit constraints. The minimum row fields are:

```text
AgentRow: id, workspace_id, name, alias, status, current_version_id, created_at
AgentDraftRow: agent_id PK, spec, revision, updated_by, updated_at
AgentVersionRow: id, agent_id, workspace_id, version_number, schema_version, spec, content_hash, published_by, created_at
SessionRow: id, workspace_id, agent_id, session_mode, caller_type, caller_id, head_run_id, next_run_sequence, next_message_sequence, workspace_revision_id, created_at
SessionMessageRow: id, session_id, workspace_id, sequence, role, content, source_run_id, redacted, created_at
RunRow: id, workspace_id, session_id, agent_version_id, status, state_version, next_event_sequence, session_sequence, blocked_by_run_id, pause_reason, pause_requested_at, cancel_requested_at, wait_kind, wait_policy, wait_deadline_at, retry_of_run_id, budget_root_run_id, checkpoint, checkpoint_replay_safe, checkpoint_effect_status, checkpoint_workspace_revision_id, started_at, finished_at, created_at, updated_at
RunBudgetScopeRow: root_run_id PK, max_execution_seconds, consumed_execution_ms, max_elapsed_seconds, elapsed_deadline_at, max_model_calls, consumed_model_calls, max_tool_calls, consumed_tool_calls, max_tokens nullable, consumed_tokens, max_derived_retries, derived_retry_count, version
RunEventRow: id, run_id, workspace_id, sequence, event_type, payload, occurred_at
WorkerLeaseRow: id, run_id UNIQUE, worker_id, acquired_at, expires_at, released_at, version
IdempotencyRecordRow: id, workspace_id, caller_type, caller_id, endpoint, idempotency_key, request_fingerprint, run_id, response_snapshot, expires_at nullable, created_at
```

`checkpoint_effect_status` is constrained to `none`, `confirmed`, or `unknown`; retry is
forbidden for `unknown`. A null `max_tokens` means this version did not request a strict
total-token budget; it is not interpreted as zero. `expires_at` stays null while its Run is
non-terminal and is set only when that Run terminalizes.

Add check constraints from the complete domain enums. Add composite uniqueness `(id, workspace_id)` on workspace-owned parents and composite foreign keys where a child stores both values. Circular Agent/current-version and Session/head-Run foreign keys use named `use_alter` constraints so downgrade order is deterministic.

- [ ] **Step 3: Generate and review the additive migration**

Import both new table modules in `migrations/env.py`, then run:

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test"
uv run --no-sync alembic revision --autogenerate -m "add agent and run foundation"
```

Alembic creates one timestamp-named file and prints its exact path. Verify that path is
inside `migrations/versions`, rename that one file to `20260810_0002_agent_runs.py`, set
`revision="20260810_0002"` and `down_revision="20260810_0001"`, and verify it contains
only additions. Review every foreign key and drop order rather than accepting generated
output blindly. If any other untracked migration exists, stop instead of guessing which
file to rename.

- [ ] **Step 4: Prove upgrade and downgrade**

Run upgrade, the focused migration test, `alembic check`, downgrade to `20260810_0001`, verify phase-one tests, and upgrade to head again.

- [ ] **Step 5: Commit**

```powershell
git add migrations packages/backend/src/tiny_hermes/agents/infrastructure packages/backend/src/tiny_hermes/runs/infrastructure packages/backend/tests/integration/test_agent_run_migration.py
git commit -m "feat: add agent and run database schema"
```

### Task 5: PostgreSQL Agent Catalog and management routes

**Files:**
- Create: `packages/backend/src/tiny_hermes/agents/infrastructure/sql_store.py`
- Create: `packages/backend/src/tiny_hermes/agents/presentation/__init__.py`
- Create: `packages/backend/src/tiny_hermes/agents/presentation/routes.py`
- Create: `packages/backend/src/tiny_hermes/identity/presentation/dependencies.py`
- Modify: `packages/backend/src/tiny_hermes/tenancy/presentation/routes.py`
- Modify: `packages/backend/src/tiny_hermes/api/resources.py`
- Modify: `packages/backend/src/tiny_hermes/api/app.py`
- Create: `packages/backend/tests/integration/agents/test_agent_api.py`

- [ ] **Step 1: Write a failing real-API flow**

Through the existing bootstrap and login APIs:

1. create a workspace;
2. `POST /api/v1/agents` with `X-Workspace-Id` and CSRF;
3. `PUT /agents/{id}/draft` with `expected_revision=1` and `valid_spec()`;
4. publish version 1;
5. publish the unchanged Draft and assert no version 2 appears;
6. change the Draft with expected revision 2 and publish version 2;
7. roll back to version 1 and assert both immutable versions remain;
8. while still authorized in both workspaces, use a second workspace ID with the first
   workspace's Agent ID and assert generic 404 without Agent name, alias, personality, or
   version hash in the response. A caller who lacks membership in the selected workspace
   receives 403 before resource lookup; test that separately.

Expected first run: 404 because Agent routes are not registered.

- [ ] **Step 2: Extract shared browser identity helpers**

Move the behavior of `_authenticate` and `_verify_write` from the workspace route into `identity/presentation/dependencies.py` as:

```python
async def authenticate_browser_user(
    auth: AuthService, session_token: str | None
) -> AuthenticatedUser: ...

async def verify_browser_write(
    auth: AuthService, session_token: str | None, csrf_token: str | None
) -> AuthenticatedUser: ...

def require_workspace_id(raw_value: str | None) -> UUID: ...
```

Keep existing error codes and responses unchanged. Refactor workspace routes to call these helpers, then run all phase-one tests before adding Agent routes.

- [ ] **Step 3: Implement the SQL adapter transaction operations**

`publish_draft` locks Agent and Draft with `SELECT ... FOR UPDATE`, validates the expected revision, normalizes the spec again server-side, compares the current version hash, and either returns the current version unchanged or inserts `max(version_number)+1` while the Agent lock serializes publishers. `activate_version` verifies the version belongs to the same Agent and workspace before updating the pointer. Every write appends an `AuditEventRow` in the same session.

- [ ] **Step 4: Add request and response models and exact routes**

Implement the phase-2A Agent routes from the design. Return `201` for Agent creation and new publication, `200` for unchanged publication and rollback, `409 draft_revision_conflict`, `403 forbidden`, and generic 404 for missing or cross-workspace resources. Published responses contain version ID, version number, schema version, content hash, and created time; error responses do not contain the spec.

- [ ] **Step 5: Assemble resources with correct transaction handling**

Add `agent_catalog()` to `ApplicationResources`, yielding `AgentCatalog(SqlAgentStore(session))`. Commit on success, roll back on ordinary exceptions, and commit only the explicit audited-denial application exception before rethrowing it. Register `agent_router(resources)` in `create_app`.

- [ ] **Step 6: Verify and commit**

Run the Agent unit tests, Agent API integration test, all phase-one tests, Ruff, and Pyright. Commit:

```powershell
git add packages/backend/src/tiny_hermes/agents packages/backend/src/tiny_hermes/identity/presentation packages/backend/src/tiny_hermes/tenancy/presentation packages/backend/src/tiny_hermes/api packages/backend/tests/integration/agents
git commit -m "feat: expose agent publication workflow"
```

### Task 6: Atomic Session and idempotent Run creation

**Files:**
- Create: `packages/backend/src/tiny_hermes/runs/application/__init__.py`
- Create: `packages/backend/src/tiny_hermes/runs/application/service.py`
- Create: `packages/backend/src/tiny_hermes/runs/ports/__init__.py`
- Create: `packages/backend/src/tiny_hermes/runs/ports/store.py`
- Create: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py`
- Create: `packages/backend/src/tiny_hermes/runs/presentation/__init__.py`
- Create: `packages/backend/src/tiny_hermes/runs/presentation/routes.py`
- Modify: `packages/backend/src/tiny_hermes/api/resources.py`
- Modify: `packages/backend/src/tiny_hermes/api/app.py`
- Create: `packages/backend/tests/integration/runs/test_run_creation.py`

- [ ] **Step 1: Write failing Session and FIFO tests**

The real-API test creates a published Agent and Session, then submits three messages with three keys. Assert:

- Session has `next_run_sequence=4` and `next_message_sequence=4`;
- Run sequences are 1, 2, 3;
- Run1 is Head with no blocker;
- Run2 and Run3 are queued and blocked by Run1;
- each Run fixes the same published Agent Version;
- each root Run owns one budget row and points `budget_root_run_id` to itself;
- each Run has exactly one `run_created` event at sequence 1 and `next_event_sequence=2`.

- [ ] **Step 2: Write the concurrent idempotency failure**

Send two concurrent `POST /api/v1/runs` calls with the same authenticated subject, Session, body, and key. Assert one returns 201 and one returns 200 with `Idempotent-Replayed: true`, both IDs match, and database counts increase by exactly one message, Run, budget, event, idempotency record, and audit success.

Repeat the key with a different message and assert 409 `idempotency_key_reused` with no new rows.

- [ ] **Step 3: Define transaction-level commands and results**

The `RunStore` interface exposes these operations rather than row CRUD:

```python
class RunStore(Protocol):
    async def create_session(self, command: CreateSessionCommand) -> SessionSnapshot: ...
    async def accept_run(self, command: AcceptRunCommand) -> AcceptedRun: ...
    async def get_run(self, workspace_id: UUID, run_id: UUID) -> RunSnapshot | None: ...
    async def list_runs(
        self, workspace_id: UUID, session_id: UUID | None
    ) -> Sequence[RunSnapshot]: ...
    async def control_run(self, command: ControlRunCommand) -> RunSnapshot: ...
    async def apply_signal(self, command: ApplySignalCommand) -> RunSnapshot: ...
    async def append_events(self, command: AppendEventsCommand) -> tuple[RunEvent, ...]: ...
    async def claim_head(self, command: ClaimRunCommand) -> ClaimedRun | None: ...
    async def repair_session_head(self, session_id: UUID, request_id: str) -> RepairResult: ...
    async def derive_retry(self, command: RetryRunCommand) -> AcceptedRun: ...
```

Commands are frozen dataclasses containing resolved workspace, stable caller type/id,
request ID, expected state version where applicable, and normalized request data.
`ApplySignalCommand` is the only seam future Worker/Scheduler code uses for state signals;
`AppendEventsCommand` reserves and writes one or more redacted event payloads in the same
transaction. Neither accepts a target state or caller-chosen event sequence.

- [ ] **Step 4: Implement request fingerprinting and `accept_run`**

Fingerprint only caller-supplied fields: method identity, endpoint, workspace, Session, canonical input message, and requested limit overrides. Do not include the Agent's mutable current-version pointer.

In one SQL transaction:

1. insert `IdempotencyRecordRow` with PostgreSQL `ON CONFLICT DO NOTHING RETURNING id`;
2. on conflict, read the committed row, compare fingerprint, and return its stored response or raise key-reused;
3. lock the Session;
4. verify its Agent has a current version and caller may start Runs;
5. allocate Run and message sequences;
6. create the UUIDs up front so the message can reference the Run;
7. insert message, Run, root budget, event, audit, and Head/pending relation;
8. store the exact creation response snapshot on the idempotency row.

Use an elapsed deadline of creation time plus the Agent limit. Initialize execution and
call consumption at zero and `derived_retry_count=0`. Phase 2A Agent specs do not request
a strict total-token ceiling, so `max_tokens` is null and `consumed_tokens` starts at zero;
future usage accounting may set a limit only through a validated Agent Version policy.

- [ ] **Step 5: Implement Session and Run routes**

Add `POST/GET /api/v1/sessions`, `GET /sessions/{id}`, `POST/GET /api/v1/runs`, and `GET /runs/{id}`. Run creation requires a non-empty `Idempotency-Key`; missing returns 400 `idempotency_key_required`. First create returns 201 and `Location`; replay returns 200 and `Idempotent-Replayed: true`.

Snapshots compute queue position from non-terminal Runs ordered before the target. Only a pending Run behind a paused or waiting Head returns `queue.status=session_blocked`; ordinary queued/running heads use normal queue information.

- [ ] **Step 6: Verify and commit**

Run the focused concurrency test repeatedly with `pytest ... --count` only if a repeat plugin is already present; otherwise use a PowerShell loop of 10 separate pytest processes. Then run all integration tests, Ruff, and Pyright. Commit:

```powershell
git add packages/backend/src/tiny_hermes/runs packages/backend/src/tiny_hermes/api packages/backend/tests/integration/runs
git commit -m "feat: accept idempotent fifo runs"
```

### Task 7: Event allocation, control, and Head Run handoff

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py`
- Modify: `packages/backend/src/tiny_hermes/runs/application/service.py`
- Modify: `packages/backend/src/tiny_hermes/runs/presentation/routes.py`
- Create: `packages/backend/tests/integration/runs/test_run_control.py`

- [ ] **Step 1: Write failing event-allocation concurrency test**

Create one Run, then use three independent database sessions representing API, Worker, and Scheduler. Each reserves two events concurrently. Assert the committed sequences are exactly `[2, 3, 4, 5, 6, 7]`, unique and gap-free, and `next_event_sequence=8`.

- [ ] **Step 2: Write the cancelled-middle regression test**

Arrange Run1 running, Run2 queued, Run3 queued. Cancel Run2 while pending, then terminalize Run1 through `RunCoordination.apply_signal`. Assert Run3 becomes Head, Run2 remains cancelled, Run3 blocker is null, and no terminal Run is selected.

- [ ] **Step 3: Implement the allocator**

Use one SQL statement per reservation:

```sql
UPDATE runs
SET next_event_sequence = next_event_sequence + :count
WHERE id = :run_id AND workspace_id = :workspace_id
RETURNING next_event_sequence - :count AS first_sequence
```

Insert the event rows with the returned contiguous range in the same transaction as the associated state change. A unique conflict rolls back; retry the full operation at most three times and then raise `event_sequence_conflict`.

- [ ] **Step 4: Implement control and handoff transactions**

`pause`, `resume`, and `cancel` routes require `expected_state_version`. Lock Run and Session, recheck workspace and permissions, call `RunStateMachine`, apply only its returned mutation, increment state version once, write event and audit, and perform terminal Head handoff before commit.

When a Run terminalizes, set `expires_at=finished_at + 24 hours` on every idempotency
record that points to it. A terminal pending Run does not trigger Head handoff; only the
Run currently referenced by `sessions.head_run_id` can hand the Session to the smallest
remaining non-terminal sequence.

Illegal controls append a redacted denied audit and raise the explicit audited-denial application error. Stale versions return `state_version_conflict`. Map routes to `/pause`, `/resume`, and `/cancel` with 200 snapshots.

- [ ] **Step 5: Verify and commit**

Run control and event tests, the Run state-machine unit suite, all integration tests, and static checks. Commit:

```powershell
git add packages/backend/src/tiny_hermes/runs packages/backend/tests/integration/runs/test_run_control.py
git commit -m "feat: control runs with atomic events"
```

### Task 8: Lease claiming and Session-head repair seam

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py`
- Modify: `packages/backend/src/tiny_hermes/runs/application/service.py`
- Create: `packages/backend/tests/integration/runs/test_run_coordination.py`

- [ ] **Step 1: Write the two-claim race**

Use two independent async sessions and one barrier to call `claim_head` for the same queued Head Run. Assert exactly one result is non-null, Run becomes running once, one unreleased lease exists, and one `run_started` event is written.

- [ ] **Step 2: Write the repair cases**

Directly corrupt test rows for each case:

- Head points to a terminal Run;
- Head is null with a pending Run;
- Head points to a later non-terminal Run;
- pending blockers point to the wrong Head.

Call the application repair seam and assert the smallest non-terminal sequence becomes Head, every pending blocker is corrected, one `session_head_repaired` AuditEvent is written, and a Run Event is written only when a new Head exists.

- [ ] **Step 3: Implement claim with row locking**

Select candidate Runs joined to Sessions with all four predicates: queued, equals Session Head, blocker null, and no unreleased unexpired lease. Use `FOR UPDATE OF runs SKIP LOCKED`, then lock the Session, call `LEASE_ACQUIRED`, upsert the one lease row with a new lease ID and version, set `expires_at=acquired_at + 30 seconds`, increment Run state version, and write the event atomically.

This slice does not renew, expire, or process the lease. It only fixes the transaction interface that phase 2B Worker will call.

- [ ] **Step 4: Implement repair with an advisory-lock-ready interface**

`repair_session_head` locks one Session and recomputes from non-terminal Runs. It is safe when called more than once: if no row changes, return `changed=False` and write no event or audit. Do not start a Scheduler loop or acquire a global advisory lock in phase 2A.

- [ ] **Step 5: Verify and commit**

Run the coordination tests 10 times, all integration tests, Ruff, and Pyright. Commit:

```powershell
git add packages/backend/src/tiny_hermes/runs packages/backend/tests/integration/runs/test_run_coordination.py
git commit -m "feat: add run claim and head repair seam"
```

### Task 9: Shared-budget safe retries

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py`
- Modify: `packages/backend/src/tiny_hermes/runs/application/service.py`
- Modify: `packages/backend/src/tiny_hermes/runs/presentation/routes.py`
- Create: `packages/backend/tests/integration/runs/test_run_retry.py`

- [ ] **Step 1: Write failing eligibility tests**

Using application seams, arrange failed Runs and assert retry is rejected when:

- checkpoint replay safety is false;
- checkpoint external effect is unknown;
- source is not the latest Run in the Session;
- Session and checkpoint workspace revision markers differ;
- elapsed deadline passed;
- model, tool, strict-token (when configured), execution, or retry limit has no remaining
  capacity.

Each rejection has its specific design error code and creates no new Run or retry count.

- [ ] **Step 2: Write sequential-chain and concurrent-boundary retry tests**

First derive Retry 1 from a failed root, make Retry 1 fail safely through
`RunCoordination.apply_signal`, derive Retry 2 from it, and make Retry 2 fail safely. Assert
both retries share the root `budget_root_run_id`, each `retry_of_run_id` points to its direct
source, and only one budget row exists with count 2.

Then send five concurrent retry requests for failed Retry 2 with five unique keys. Exactly
one request consumes the last slot and creates Retry 3; the others return
`retry_limit_reached`. This arrangement preserves the separate rule that a retry source
must be the latest Run in its Session while still proving the root count cannot cross 3
under concurrency. A fourth sequential derivation also returns `retry_limit_reached`.

Replay the successful Retry 3 key and assert it returns that Derived Retry without
incrementing again.

- [ ] **Step 3: Implement retry in one locked transaction**

Compete for retry idempotency first, then lock source Run, Session, and root budget in that order. Recheck role, source status, latest sequence, checkpoint flags, revision marker, and all remaining limits. Increment the root count with a version predicate. Allocate new Session/message sequences, copy only the authorized checkpoint message references, create the Derived Retry with the original Agent Version and budget root, establish Head/pending relations, and write source/new events plus audit.

The source Run remains failed and immutable. Interrupted recovery uses the normal state signal and never calls this route.

- [ ] **Step 4: Expose retry and available actions**

`POST /api/v1/runs/{run_id}/retry` requires `Idempotency-Key` and returns 201/200 with the same replay headers as root creation. Failed Run snapshots contain `retry` only after the server has checked safety, latest-Run position, current revision marker, role, and remaining root budget.

- [ ] **Step 5: Verify and commit**

Run retry tests repeatedly, the complete Run suite, all integration tests, and static checks. Commit:

```powershell
git add packages/backend/src/tiny_hermes/runs packages/backend/tests/integration/runs/test_run_retry.py
git commit -m "feat: derive retries from shared budgets"
```

### Task 10: Full HTTP tracer flow, CI, docs, and exit record

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/development.md`
- Create: `docs/superpowers/verification/2026-08-10-m1-run-foundation.md`
- Create: `packages/backend/tests/integration/runs/test_run_api_flow.py`

- [ ] **Step 1: Add the complete failing HTTP flow**

The test uses only public APIs for bootstrap, login, workspace, Agent, Draft, publish, Session, Run create/replay, queued pause/resume/cancel, Run list, and safe-retry errors. It may call the application seam to represent a future Worker terminal signal, but may not mutate database state directly except in tests explicitly named as corruption/repair tests.

Assert every write has an AuditEvent, every cross-workspace attempt is generic, and snapshots consistently report Agent Version, Head, blocker, queue position, state version, budget root, retry source, and available actions.

- [ ] **Step 2: Extend CI integration coverage**

Keep the four existing jobs. In `backend-integration`, retain upgrade/test/check/downgrade/upgrade and add no service other than PostgreSQL for phase 2A. Add a 10-iteration PowerShell-equivalent Linux shell loop for the focused idempotency, event allocation, claim, and retry concurrency tests:

```yaml
- name: Repeat concurrency regressions
  run: |
    for attempt in $(seq 1 10); do
      uv run pytest \
        packages/backend/tests/integration/runs/test_run_creation.py \
        packages/backend/tests/integration/runs/test_run_control.py \
        packages/backend/tests/integration/runs/test_run_coordination.py \
        packages/backend/tests/integration/runs/test_run_retry.py -q
    done
```

- [ ] **Step 3: Update development documentation**

Add exact authenticated curl or PowerShell examples for creating Agent/Draft/version, Session, and idempotent Run. State plainly that Runs remain queued in phase 2A because Worker, SSE, model, and sandbox arrive in later batches. Include migration and focused test commands.

- [ ] **Step 4: Run the complete fresh verification**

Run:

```powershell
$env:UV_CACHE_DIR = ".uv-cache"
uv sync --frozen
uv run --no-sync ruff check packages/backend migrations
uv run --no-sync pyright
uv run --no-sync pytest packages/backend/tests/unit -v

$env:DATABASE_URL = "postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test"
$env:TEST_DATABASE_URL = $env:DATABASE_URL
uv run --no-sync alembic upgrade head
uv run --no-sync pytest packages/backend/tests/integration -v
uv run --no-sync alembic check
uv run --no-sync alembic downgrade 20260810_0001
uv run --no-sync alembic upgrade head

corepack pnpm install --frozen-lockfile
corepack pnpm web:lint
corepack pnpm web:test
corepack pnpm web:build
docker compose -f deploy/compose/compose.yaml up -d --build --wait
docker compose -f deploy/compose/compose.yaml ps -a
```

Run tracked-file secret scanning with secret-shaped minimum lengths and `git diff --check`. Do not reset or delete any Docker volume without first resolving and reporting its exact project-owned names.

- [ ] **Step 5: Write the verification record**

Record commit ID, versions, commands, counts, concurrency repetitions, migration round trip, known phase-2A limits, and redacted failure evidence. Do not copy cookies, passwords, bootstrap tokens, request bodies containing personality text, or database URLs.

- [ ] **Step 6: Commit the verified slice**

```powershell
git add .github/workflows/ci.yml docs/development.md docs/superpowers/verification/2026-08-10-m1-run-foundation.md packages/backend/tests/integration/runs/test_run_api_flow.py
git commit -m "ci: verify run foundation transactions"
git status --short
```

Expected: commit succeeds and status is clean.

## 3. Phase 2A completion checklist

- [ ] Agent Draft revisions and immutable Agent Versions are enforced.
- [ ] Repeated unchanged publish does not create duplicate versions.
- [ ] Session Head and pending FIFO invariants hold under cancellation and terminal handoff.
- [ ] Run creation is database-first idempotent under concurrent requests.
- [ ] Run Event sequences are contiguous across concurrent writer identities.
- [ ] The full v2.4 state matrix is represented only by `RunStateMachine`.
- [ ] Controls use state versions and denied operations are audited.
- [ ] Two claimers cannot obtain the same Run.
- [ ] Head repair is idempotent and audited only when it changes data.
- [ ] Derived Retries share one root budget and concurrent requests cannot push the
      default count above three.
- [ ] Workspace selection never substitutes for membership and ownership verification.
- [ ] All old and new tests, migrations, static checks, Web checks, Compose readiness, and secret scans pass.
- [ ] No phase-2B or phase-2C capability is represented as working before it exists.
