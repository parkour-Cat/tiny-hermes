# M1 Run Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver phase 2B: a Worker that executes Runs against a deterministic model substitute, a Scheduler that reclaims and repairs, a Redis wake-up that only shortens latency, and an SSE stream that resumes without gaps and refuses stale cursors.

**Architecture:** Add `worker` and `scheduler` processes to the existing Python release unit. Both drive the phase-2A `RunStore` interface and nothing else; `RunStateMachine` still makes every state decision. The model lives behind a `ModelProvider` port whose only phase-2B adapter is deterministic. PostgreSQL remains the sole truth; Redis carries notifications only.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2 async, PostgreSQL 17, Redis 8, Alembic, pytest, pytest-asyncio, httpx, Docker Compose

---

## 1. Fixed scope and working rules

- Work only in `.worktrees/m1-run-execution` on branch `m1-run-execution`.
- Treat the product design v2.4, the M1 technical design v1.1, and
  `docs/superpowers/specs/2026-08-10-m1-run-execution-design.md` as authoritative.
- Consume the phase-2A `RunStore` interface as committed. Extend it only with the
  two commands this plan names; do not reshape existing commands.
- Use `uv run --no-sync` after the initial locked install and `corepack pnpm` for
  Node commands.
- Write and observe a failing test before each production behavior.
- Do not create tools, files, sandbox, SessionWorkspace, Chat Completions,
  ServiceAccount, real model endpoint, or phase-2C UI placeholders.
- Every database integration command targets only `tiny_hermes_test`.
- Both long-running processes are built as testable runtimes with a `run_once()`
  method. Tests call `run_once()`; only the console script loops. No test spawns
  or kills an operating-system process.
- Tests shorten durations through settings instead of sleeping for production
  defaults. No test may sleep longer than two seconds.
- Commit after each task only when its focused checks pass.

## 2. File map

```text
packages/backend/src/tiny_hermes/
├─ shared/config.py                              # new bounded execution settings
├─ runs/
│  ├─ domain/slice_policy.py                     # pure end-of-round decision
│  ├─ domain/models.py                           # RunEventType gains run_limit_reached
│  ├─ ports/model.py                             # ModelProvider seam
│  ├─ ports/store.py                             # RenewLeaseCommand, RecordSliceCommand
│  ├─ ports/notifier.py                          # wake-up seam
│  ├─ infrastructure/deterministic_model.py      # the only phase-2B provider
│  ├─ infrastructure/redis_notifier.py           # publish/subscribe, failure tolerant
│  ├─ infrastructure/null_notifier.py            # used when Redis is not configured
│  ├─ infrastructure/sql_store.py                # renew_lease, record_slice, scans
│  ├─ application/worker.py                      # WorkerRuntime
│  ├─ application/scheduler.py                   # SchedulerRuntime
│  └─ presentation/events.py                     # SSE route
├─ api/{resources,app,cli}.py                    # wiring plus two console scripts
migrations/versions/20260810_0003_run_execution.py
packages/backend/tests/
├─ unit/runs/test_deterministic_model.py
├─ unit/runs/test_slice_policy.py
├─ unit/shared/test_config.py                    # extended
├─ integration/test_run_execution_migration.py
├─ integration/runs/test_worker_slice.py
├─ integration/runs/test_worker_execution.py
├─ integration/runs/test_wakeup.py
├─ integration/runs/test_scheduler.py
├─ integration/runs/test_run_events_sse.py
└─ integration/runs/test_execution_flow.py
```

---

### Task 1: Bounded execution settings and the deterministic model

**Files:**
- Modify: `packages/backend/src/tiny_hermes/shared/config.py`
- Create: `packages/backend/src/tiny_hermes/runs/ports/model.py`
- Create: `packages/backend/src/tiny_hermes/runs/infrastructure/deterministic_model.py`
- Create: `packages/backend/tests/unit/runs/test_deterministic_model.py`
- Modify: `packages/backend/tests/unit/shared/test_config.py`

- [ ] **Step 1: Write the failing provider tests**

Create `packages/backend/tests/unit/runs/test_deterministic_model.py`. The
provider is chosen by the scenario already validated into every Agent Version,
so the test drives it from a real `AgentSpec`:

```python
import pytest
from tiny_hermes.agents.domain.models import AgentSpec
from tiny_hermes.runs.infrastructure.deterministic_model import (
    DeterministicModelProvider,
)
from tiny_hermes.runs.ports.model import ModelRequest, StopReason

from ..agents.test_agent_models import valid_spec


def _request(scenario: str, round_index: int) -> ModelRequest:
    values = {**valid_spec(), "model_policy": {"provider": "deterministic", "scenario": scenario}}
    spec = AgentSpec.model_validate(values)
    return ModelRequest(
        policy=spec.model_policy,
        personality=spec.personality,
        input_text="do the thing",
        round_index=round_index,
    )


async def test_complete_finishes_in_one_round() -> None:
    response = await DeterministicModelProvider(delay_ms=0).complete(_request("complete", 1))

    assert response.stop_reason is StopReason.COMPLETED
    assert response.text
    assert response.replay_safe is True
    assert response.model_calls == 1


async def test_fail_replay_safe_fails_without_an_unknown_effect() -> None:
    response = await DeterministicModelProvider(delay_ms=0).complete(
        _request("fail_replay_safe", 1)
    )

    assert response.stop_reason is StopReason.FAILED
    assert response.replay_safe is True
    assert response.external_effect_unknown is False


async def test_continue_once_needs_a_second_round() -> None:
    provider = DeterministicModelProvider(delay_ms=0)

    first = await provider.complete(_request("continue_once", 1))
    second = await provider.complete(_request("continue_once", 2))

    assert first.stop_reason is StopReason.CONTINUE
    assert second.stop_reason is StopReason.COMPLETED


async def test_the_provider_is_pure_for_the_same_round() -> None:
    provider = DeterministicModelProvider(delay_ms=0)
    first = await provider.complete(_request("complete", 1))
    second = await provider.complete(_request("complete", 1))

    assert first == second


@pytest.mark.parametrize("delay", [-1, 5001])
def test_the_delay_is_bounded(delay: int) -> None:
    with pytest.raises(ValueError, match="delay"):
        DeterministicModelProvider(delay_ms=delay)
```

Add settings tests to `packages/backend/tests/unit/shared/test_config.py` in the
existing style: every new setting rejects a value outside its documented range
and accepts its default.

- [ ] **Step 2: Verify red**

```powershell
$env:UV_CACHE_DIR = ".uv-cache"
uv run --no-sync pytest packages/backend/tests/unit/runs/test_deterministic_model.py -v
```

Expected: collection fails because `tiny_hermes.runs.ports.model` does not exist.

- [ ] **Step 3: Define the model port**

`runs/ports/model.py` holds frozen dataclasses and one Protocol. It imports the
Agent policy value object but nothing from infrastructure:

```python
class StopReason(StrEnum):
    COMPLETED = "completed"
    CONTINUE = "continue"
    FAILED = "failed"


@dataclass(frozen=True)
class ModelRequest:
    policy: DeterministicModelPolicy
    personality: str
    input_text: str
    round_index: int


@dataclass(frozen=True)
class ModelResponse:
    stop_reason: StopReason
    text: str
    model_calls: int = 1
    tokens: int = 0
    replay_safe: bool = True
    external_effect_unknown: bool = False


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
```

`replay_safe` and `external_effect_unknown` exist now so phase 3 can report an
uncertain side effect truthfully without reshaping the port.

- [ ] **Step 4: Implement the deterministic provider**

The provider validates `delay_ms` in `0..5000` at construction, sleeps that long,
and maps `(scenario, round_index)` to a fixed response using the table in the
design document §4.3. `continue_once` returns `CONTINUE` for round 1 and
`COMPLETED` for every later round. It performs no I/O other than
`asyncio.sleep`, imports no HTTP client, and is not a test double: it is the
provider phase 2B ships.

- [ ] **Step 5: Add the bounded settings**

Add to `Settings` with the same `Field(default=..., ge=..., le=...)` style
already used by `session_ttl_seconds`, matching design document §10:
`worker_lease_seconds`, `worker_max_slice_seconds`, `worker_idle_poll_seconds`,
`worker_shutdown_grace_seconds`, `scheduler_interval_seconds`,
`max_recovery_attempts`, `event_retention_hours`, `sse_heartbeat_seconds`,
`deterministic_model_delay_ms`. Add `redis` to `[project.dependencies]` and
refresh `uv.lock` with `uv lock`.

- [ ] **Step 6: Verify green and commit**

```powershell
uv run --no-sync pytest packages/backend/tests/unit -v
uv run --no-sync ruff check packages/backend
uv run --no-sync pyright
git add packages/backend/src/tiny_hermes/runs/ports/model.py packages/backend/src/tiny_hermes/runs/infrastructure/deterministic_model.py packages/backend/src/tiny_hermes/shared/config.py packages/backend/tests/unit pyproject.toml uv.lock
git commit -m "feat: add deterministic model provider"
```

### Task 2: Execution columns and the safety-valve event

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/domain/models.py`
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/tables.py`
- Create: `migrations/versions/20260810_0003_run_execution.py`
- Create: `packages/backend/tests/integration/test_run_execution_migration.py`

- [ ] **Step 1: Write the failing migration test**

Assert against a database at head:

- `runs.recovery_attempts` exists, is non-null, and defaults to `0`;
- `runs.last_heartbeat_at` exists and is nullable;
- the `ck_run_events_event_type` constraint accepts `run_limit_reached` by
  inserting one such event for a seeded Run and rolling back;
- the constraint still rejects an invented `run_not_a_real_event` value;
- downgrade to `20260810_0002` drops both columns, restores the narrower
  constraint, and preserves every phase-2A table.

- [ ] **Step 2: Add the domain value first**

`RunEventType` gains an explicit `RUN_LIMIT_REACHED = "run_limit_reached"`
member with a comment stating why it is the one name not derived from
`RunSignal`: it records the safety valve required by product design v2.4 §12.3,
which is a budget fact rather than a state transition. `event_type_for` is
unchanged and still refuses to invent names.

- [ ] **Step 3: Declare the row changes**

Add to `RunRow`:

```python
recovery_attempts: Mapped[int] = mapped_column(Integer, default=0)
last_heartbeat_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
```

Add a check constraint `ck_runs_recovery_attempts` for `recovery_attempts >= 0`.
The event-type constraint text is regenerated from the widened enum, so it stays
mechanically derived rather than hand-listed.

- [ ] **Step 4: Write the migration by hand**

Do not autogenerate this one. It is small and the phase-2A verification record
already documents that autogenerate silently drops constraints it cannot render
inline. Write:

```python
def upgrade() -> None:
    op.add_column("runs", sa.Column("recovery_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("runs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_runs_recovery_attempts", "runs", "recovery_attempts >= 0")
    op.drop_constraint("ck_run_events_event_type", "run_events", type_="check")
    op.create_check_constraint("ck_run_events_event_type", "run_events", EVENT_TYPES)
```

`EVENT_TYPES` is the widened literal list, written out in the migration file the
same way `20260810_0002` writes it. Downgrade reverses all four operations and
restores the phase-2A list verbatim. Drop `server_default` after backfill is not
needed because the column is new and every row gets `0`.

- [ ] **Step 5: Prove the round trip**

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test"
$env:TEST_DATABASE_URL = $env:DATABASE_URL
uv run --no-sync alembic upgrade head
uv run --no-sync pytest packages/backend/tests/integration/test_run_execution_migration.py -v
uv run --no-sync alembic check
uv run --no-sync alembic downgrade 20260810_0002
uv run --no-sync pytest packages/backend/tests/integration/test_agent_run_migration.py -v
uv run --no-sync alembic upgrade head
```

`alembic check` must report no new operations. If it does not, the SQLAlchemy
rows and the migration disagree; fix the rows, never the check.

- [ ] **Step 6: Commit**

```powershell
git add migrations packages/backend/src/tiny_hermes/runs packages/backend/tests/integration/test_run_execution_migration.py
git commit -m "feat: add run execution columns"
```

### Task 3: The pure slice policy

**Files:**
- Create: `packages/backend/src/tiny_hermes/runs/domain/slice_policy.py`
- Create: `packages/backend/tests/unit/runs/test_slice_policy.py`

- [ ] **Step 1: Write the failing precedence table**

The policy answers one question: after a round ends, what happens next? It
returns a signal or `None` for "keep going in this lease". The precedence in
design document §6.3 is the whole point, so the test is table-driven and
exhaustive over the interesting combinations:

```python
from tiny_hermes.runs.domain.models import PauseReason, RunSignal
from tiny_hermes.runs.domain.slice_policy import RoundOutcome, SliceDecision, decide_after_round
from tiny_hermes.runs.ports.model import StopReason

CASES = [
    # cancel wins over everything, including a model that finished
    (RoundOutcome(StopReason.COMPLETED, cancel_requested=True, pause_requested=True,
                  budget_allows=True, slice_expired=True), RunSignal.SAFE_CANCEL_STARTED, None),
    # pause wins over a model that wants to continue and over slice expiry
    (RoundOutcome(StopReason.CONTINUE, cancel_requested=False, pause_requested=True,
                  budget_allows=True, slice_expired=True), RunSignal.SAFE_PAUSE_REACHED,
     PauseReason.MANUAL),
    # the safety valve wins over the model
    (RoundOutcome(StopReason.CONTINUE, cancel_requested=False, pause_requested=False,
                  budget_allows=False, slice_expired=False), RunSignal.SAFE_PAUSE_REACHED,
     PauseReason.LIMIT),
    (RoundOutcome(StopReason.COMPLETED, cancel_requested=False, pause_requested=False,
                  budget_allows=True, slice_expired=False), RunSignal.COMPLETED, None),
    (RoundOutcome(StopReason.FAILED, cancel_requested=False, pause_requested=False,
                  budget_allows=True, slice_expired=False), RunSignal.FAILED, None),
    (RoundOutcome(StopReason.CONTINUE, cancel_requested=False, pause_requested=False,
                  budget_allows=True, slice_expired=True), RunSignal.SLICE_ENDED, None),
]


@pytest.mark.parametrize(("outcome", "signal", "reason"), CASES)
def test_precedence(outcome, signal, reason) -> None:
    decision = decide_after_round(outcome)
    assert decision.signal is signal
    assert decision.pause_reason is reason


def test_a_continuing_round_inside_budget_and_slice_keeps_the_lease() -> None:
    decision = decide_after_round(
        RoundOutcome(StopReason.CONTINUE, cancel_requested=False, pause_requested=False,
                     budget_allows=True, slice_expired=False)
    )
    assert decision.signal is None
    assert decision.keeps_lease is True


def test_only_the_limit_pause_records_the_safety_valve_event() -> None:
    limited = decide_after_round(
        RoundOutcome(StopReason.CONTINUE, cancel_requested=False, pause_requested=False,
                     budget_allows=False, slice_expired=False)
    )
    manual = decide_after_round(
        RoundOutcome(StopReason.CONTINUE, cancel_requested=False, pause_requested=True,
                     budget_allows=True, slice_expired=False)
    )
    assert limited.limit_reached is True
    assert manual.limit_reached is False
```

Add a completeness test that iterates every combination of the four booleans
against all three stop reasons and asserts the returned signal is always one of
the five documented outcomes or `None`, and that `None` occurs only when the
model said `CONTINUE`, nothing was requested, the budget allows, and the slice
has not expired.

- [ ] **Step 2: Verify red, then implement**

`slice_policy.py` contains no I/O, no clock, and no database type. It receives a
`RoundOutcome` of plain values and returns a frozen `SliceDecision` with
`signal`, `pause_reason`, `limit_reached`, and `keeps_lease`. It never chooses a
target state; the signal it returns still goes through `RunStateMachine`.

The cancel case returns `SAFE_CANCEL_STARTED` only. The caller is responsible for
following it with `SAFE_CANCEL_FINISHED`, because deciding that phase 2B has no
cleanup work is a Worker fact, not a domain rule.

- [ ] **Step 3: Verify green and commit**

```powershell
uv run --no-sync pytest packages/backend/tests/unit -v
uv run --no-sync ruff check packages/backend
uv run --no-sync pyright
git add packages/backend/src/tiny_hermes/runs/domain/slice_policy.py packages/backend/tests/unit/runs/test_slice_policy.py
git commit -m "feat: decide execution slice boundaries"
```

### Task 4: Lease renewal and the atomic slice record

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/ports/store.py`
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py`
- Modify: `packages/backend/src/tiny_hermes/runs/application/service.py`
- Create: `packages/backend/tests/integration/runs/test_worker_slice.py`

- [ ] **Step 1: Write the failing store tests**

Using the phase-2A fixtures and a claimed Run, assert:

- `renew_lease` extends `expires_at`, increments `worker_leases.version`, and
  sets `runs.last_heartbeat_at`;
- `renew_lease` returns `None` when the lease was already reclaimed, released, or
  belongs to another worker, and writes nothing in that case;
- `record_slice` with `SLICE_ENDED` moves the Run back to `queued`, sets
  `released_at` on the lease, adds the reported milliseconds to
  `run_budget_scopes.consumed_execution_ms`, increments `consumed_model_calls`
  and `consumed_tokens`, bumps the budget `version`, increments the Run's
  `state_version` exactly once, and writes exactly one event;
- `record_slice` with `COMPLETED` also releases the lease, hands the Session head
  to the next non-terminal Run, and sets `expires_at` on the idempotency record;
- `record_slice` with `limit_reached=True` writes both the pause event and one
  `run_limit_reached` event with contiguous sequences;
- `record_slice` rejects a stale lease version with `LeaseLost` and leaves the
  Run, the lease, and the budget untouched;
- two concurrent `record_slice` calls for the same Run produce exactly one
  successful accounting, proving execution time cannot be double counted.

- [ ] **Step 2: Define the two commands**

Add to `runs/ports/store.py`:

```python
@dataclass(frozen=True)
class RenewLeaseCommand:
    workspace_id: UUID
    run_id: UUID
    lease_id: UUID
    expected_version: int
    lease_seconds: int


@dataclass(frozen=True)
class RecordSliceCommand:
    """One checkpoint, its accounting, its state signal, and the lease release."""

    workspace_id: UUID
    run_id: UUID
    lease_id: UUID
    expected_lease_version: int
    expected_state_version: int
    signal: RunSignal | None
    pause_reason: PauseReason | None
    limit_reached: bool
    checkpoint: dict[str, Any]
    checkpoint_replay_safe: bool
    checkpoint_effect_status: CheckpointEffectStatus
    executed_ms: int
    model_calls: int
    tokens: int
    request_id: str
    capabilities: RunCapabilities


@dataclass(frozen=True)
class RenewedLease:
    lease_id: UUID
    version: int
    expires_at: datetime
```

Extend the `RunStore` Protocol with `renew_lease` and `record_slice`. Add
`LeaseLost(RunCoordinationError)` to the application service.

- [ ] **Step 3: Implement both in one transaction each**

`renew_lease` is a single conditional `UPDATE ... RETURNING` on
`worker_leases` predicated on id, run, version, and `released_at IS NULL`, plus
the `runs.last_heartbeat_at` write. No row lock is needed because the predicate
is the concurrency control.

`record_slice` runs in this order:

1. lock the Run, then the Session, in the order phase 2A already established;
2. verify the lease still matches id and version and is unreleased, else
   `LeaseLost`;
3. verify `state_version`, else `StateVersionConflict`;
4. save the checkpoint columns;
5. add `executed_ms`, `model_calls`, and `tokens` to the root budget with a
   version predicate, so a lost update is impossible;
6. when `signal` is not `None`, apply it through the same internal path
   `apply_signal` uses, so `RunStateMachine` still decides and the terminal head
   handoff still happens;
7. when `limit_reached`, append the `run_limit_reached` event in the same
   reservation as the state event, so the two sequences are contiguous;
8. release the lease when the signal ends the slice, and leave it held when
   `signal` is `None`.

Refactor `apply_signal`'s body into a private helper both methods call rather
than duplicating the decide-and-write logic. That refactor is the point: there
must remain exactly one place that turns a decision into rows.

- [ ] **Step 4: Verify and commit**

```powershell
uv run --no-sync pytest packages/backend/tests/integration/runs/test_worker_slice.py -v
uv run --no-sync pytest packages/backend/tests -q
uv run --no-sync ruff check packages/backend
uv run --no-sync pyright
git add packages/backend/src/tiny_hermes/runs packages/backend/tests/integration/runs/test_worker_slice.py
git commit -m "feat: record execution slices atomically"
```

### Task 5: The Worker runtime

**Files:**
- Create: `packages/backend/src/tiny_hermes/runs/application/worker.py`
- Modify: `packages/backend/src/tiny_hermes/api/cli.py`
- Modify: `pyproject.toml`
- Create: `packages/backend/tests/integration/runs/test_worker_execution.py`

- [ ] **Step 1: Write the failing execution tests**

Add a conftest helper that publishes an Agent with a chosen scenario, so tests
read as behavior rather than setup:

```python
@pytest.fixture
def agent_with_scenario(client, scope):
    def publish(scenario: str, alias: str = "runner") -> str:
        ...  # create agent, PUT draft with that scenario, publish version 1
    return publish
```

Then assert, all through the public API plus `WorkerRuntime.run_once()`:

- a `complete` Run reaches `completed` with no manual signal, has
  `started_at` and `finished_at`, a released lease, and the event sequence
  `run_created, run_lease_acquired, run_completed` with no gaps;
- a `continue_once` Run needs two `run_once()` calls: the first ends with
  `run_slice_ended` and returns it to `queued` with the lease released and the
  Run still Head; the second completes it. Total execution milliseconds are the
  sum of both slices, and `consumed_model_calls` is 2;
- a `fail_replay_safe` Run reaches `failed` with `checkpoint_replay_safe=true`
  and `checkpoint_effect_status='none'`, its snapshot offers `retry`, and the
  phase-2A retry route then derives a Run that a further `run_once()` executes;
- a Run paused through the HTTP route while `running` stays `running` with
  `pause_requested_at` set, and becomes `paused(manual)` after the next
  `run_once()`;
- a Run cancelled through the HTTP route while `running` becomes `cancelled`
  after the next `run_once()`, and the Session hands off;
- a Run whose root budget is already exhausted becomes `paused(limit)`, writes
  `run_limit_reached`, and made no model call;
- `run_once()` returns `None` and writes nothing when no Run is claimable;
- only the Head Run is executed: with three Runs queued, three `run_once()`
  calls execute them in `session_sequence` order and never two at once.

- [ ] **Step 2: Implement `WorkerRuntime`**

```python
class WorkerRuntime:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        model: ModelProvider,
        notifier: WakeUpNotifier,
        settings: WorkerSettings,
        clock: Callable[[], datetime] = ...,
    ) -> None: ...

    async def run_once(self) -> UUID | None: ...
    async def run_forever(self, stop: asyncio.Event) -> None: ...
```

`run_once` claims through `RunCoordination`, then executes rounds until the
slice policy returns a signal, then calls `record_slice` once. Each round:

1. build a `ModelRequest` from the Run's fixed Agent Version, not from the
   Agent's current pointer, so a publish mid-flight cannot change behavior;
2. call the provider;
3. re-read the control flags and budget for the Run inside the same store;
4. ask `decide_after_round`;
5. keep going or stop.

The renewal task runs concurrently with the rounds and is always cancelled in a
`finally`. If renewal reports the lease is gone, the round loop is abandoned
without writing, exactly as design document §6.2 requires.

`run_forever` loops `run_once`, waiting on the notifier for at most
`worker_idle_poll_seconds` when nothing was claimable, and exits cleanly when
the stop event is set. Add a `tiny-hermes-worker` console script that builds
settings, a session factory, the deterministic provider, and a notifier, then
installs a `SIGTERM` handler that sets the stop event.

- [ ] **Step 3: Verify and commit**

Run the new file, then the whole suite, Ruff, and Pyright.

```powershell
git add packages/backend/src/tiny_hermes/runs/application/worker.py packages/backend/src/tiny_hermes/api/cli.py pyproject.toml packages/backend/tests/integration/runs/test_worker_execution.py
git commit -m "feat: execute runs in worker slices"
```

### Task 6: Redis wake-up that only saves latency

**Files:**
- Create: `packages/backend/src/tiny_hermes/runs/ports/notifier.py`
- Create: `packages/backend/src/tiny_hermes/runs/infrastructure/redis_notifier.py`
- Create: `packages/backend/src/tiny_hermes/runs/infrastructure/null_notifier.py`
- Modify: `packages/backend/src/tiny_hermes/api/resources.py`
- Create: `packages/backend/tests/integration/runs/test_wakeup.py`

- [ ] **Step 1: Write the failing wake-up tests**

- publishing after a committed Run acceptance delivers one notification carrying
  only a workspace ID and a Run ID, and no message text;
- a Worker waiting on the notifier returns as soon as a notification arrives,
  and returns after the timeout when none does;
- **with the notifier pointed at an unreachable Redis URL, creating a Run still
  returns `201`, publishing logs and swallows the failure, and
  `WorkerRuntime.run_once()` still finds and completes the Run.** This test is
  the point of the task; without it Redis has quietly become a dependency.
- a notification for an already-terminal Run causes no claim and no error.

- [ ] **Step 2: Define the seam and both adapters**

```python
class WakeUpNotifier(Protocol):
    async def publish(self, workspace_id: UUID, run_id: UUID) -> None: ...
    async def wait(self, timeout_seconds: float) -> bool: ...
    async def close(self) -> None: ...
```

`RedisWakeUpNotifier` wraps `redis.asyncio` pub/sub on one channel. Every
`publish` is wrapped so that any connection error is logged at warning level and
swallowed; the business transaction has already committed and a lost
notification is only latency. `wait` returns `False` on timeout or on any
connection error, which makes the caller fall back to polling.

`NullWakeUpNotifier` publishes nothing and sleeps out the timeout. It is used
when no Redis URL is configured and by tests that must prove independence.

- [ ] **Step 3: Publish only after commit**

`ApplicationResources.run_coordination` yields the service as before; the
notification is sent by the route after the dependency has committed, never
inside the transaction. Add the same publish after a Worker slice returns a Run
to `queued` and after a Scheduler recovery makes one claimable.

- [ ] **Step 4: Verify and commit**

```powershell
uv run --no-sync pytest packages/backend/tests/integration/runs/test_wakeup.py -v
git add packages/backend/src/tiny_hermes/runs packages/backend/src/tiny_hermes/api/resources.py packages/backend/tests/integration/runs/test_wakeup.py
git commit -m "feat: wake workers without trusting redis"
```

### Task 7: The Scheduler runtime

**Files:**
- Create: `packages/backend/src/tiny_hermes/runs/application/scheduler.py`
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py`
- Modify: `packages/backend/src/tiny_hermes/api/cli.py`
- Create: `packages/backend/tests/integration/runs/test_scheduler.py`

- [ ] **Step 1: Write the failing scan tests**

- a Run claimed with a one-second lease that is never renewed becomes
  `interrupted` after the lease expires and one `run_once()`, with the reason in
  the event payload and the lease marked released;
- that same Run then returns to `queued` with `recovery_attempts = 1`, and a
  Worker finishes it, proving no committed state was lost and no event sequence
  was skipped;
- a Run whose checkpoint is not replay safe, or whose effect status is
  `unknown`, or whose budget is exhausted, takes `RECOVERY_FAILED` and becomes
  `failed` instead of looping;
- a Run reaching `max_recovery_attempts` becomes `failed`;
- expired idempotency records are deleted and non-expired ones are not;
- `run_events` for terminal Runs older than the retention window are deleted and
  events for non-terminal Runs are never touched;
- a corrupted Session head is repaired through the phase-2A seam and audited
  once;
- a second immediate `run_once()` changes nothing, writes no audit, and writes
  no event;
- two `SchedulerRuntime.run_once()` calls racing under the advisory lock produce
  exactly one repair, and the loser skips rather than blocking.

- [ ] **Step 2: Add the scan store operations**

Add to `SqlRunStore`, each one transaction:

- `expired_lease_runs(now, limit)` returning candidate Run IDs;
- `recover_interrupted(command)` applying `RECOVERY_APPROVED` or
  `RECOVERY_FAILED` after checking replay safety, effect status, budget, and
  `recovery_attempts`, incrementing the counter in the same transaction;
- `sessions_needing_repair(limit)` returning Session IDs whose head is terminal,
  null with pending Runs, not the smallest non-terminal sequence, or whose
  pending blockers are wrong;
- `delete_expired_idempotency_records(now, limit)`;
- `prune_terminal_run_events(before, limit)`;
- `try_scan_lock(name)` wrapping `pg_try_advisory_xact_lock`.

Every scan is bounded by a limit so one cycle cannot hold a transaction open
across the whole table.

- [ ] **Step 3: Implement `SchedulerRuntime`**

`run_once()` executes each scan family in its own transaction, each guarded by
`try_scan_lock`. A family whose lock is unavailable is skipped for this cycle
and logged at debug level; it is never awaited. `run_forever` sleeps
`scheduler_interval_seconds` between cycles and honours a stop event. Add a
`tiny-hermes-scheduler` console script.

`expired_lease_runs` → `interrupted` uses `apply_signal`, so the state machine
still decides. The design's sandbox precondition is recorded as a comment naming
phase 3, not as a fake check.

The `wait_deadline_at` scan is implemented and tested through the signal seam,
and its docstring states plainly that phase 2B never produces a
`waiting_external` Run, so the scan is dormant until phase 3.

- [ ] **Step 4: Verify and commit**

```powershell
uv run --no-sync pytest packages/backend/tests/integration/runs/test_scheduler.py -v
uv run --no-sync pytest packages/backend/tests -q
git add packages/backend/src/tiny_hermes/runs packages/backend/src/tiny_hermes/api/cli.py packages/backend/tests/integration/runs/test_scheduler.py
git commit -m "feat: reclaim and repair runs on a schedule"
```

### Task 8: The SSE event stream

**Files:**
- Create: `packages/backend/src/tiny_hermes/runs/presentation/events.py`
- Modify: `packages/backend/src/tiny_hermes/runs/application/service.py`
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py`
- Modify: `packages/backend/src/tiny_hermes/api/app.py`
- Create: `packages/backend/tests/integration/runs/test_run_events_sse.py`

- [ ] **Step 1: Write the failing stream tests**

Use `client.stream("GET", ...)` so the assertions are on a real streaming
response:

- an unauthenticated request returns `401` and a missing `workspace_id` returns
  `400 workspace_required`;
- a cross-workspace Run ID returns a generic `404 run_not_found` and leaks no
  Agent name, alias, or personality;
- subscribing to a completed Run yields every event in sequence order, each
  frame carrying `id`, `event`, and JSON `data`, and then closes;
- resuming with `Last-Event-ID: 2` yields only sequences greater than two, with
  no gap and no duplicate;
- `last_event_id` as a query parameter behaves identically, and the header wins
  when both are present;
- after pruning a terminal Run's early events, a cursor below the earliest
  retained sequence returns `410` with `earliest_available_sequence` and the Run
  snapshot URL in the Problem Details body, and no partial stream is sent;
- a Run whose events were pruned entirely also returns `410` rather than an
  empty successful stream;
- a live Run streams the events a `WorkerRuntime.run_once()` produces and then
  ends when the Run terminalizes;
- disconnecting mid-stream leaves no open transaction: assert
  `pg_stat_activity` shows no `idle in transaction` connection for the test
  database afterwards.

- [ ] **Step 2: Add the read operations**

Add `list_events_after(workspace_id, run_id, after_sequence, limit)` and
`event_window(workspace_id, run_id)` returning the earliest retained sequence
and `next_event_sequence`. Both are ordinary reads in their own short
transactions. Add `RunEventCursorTooOld(RunCoordinationError)` carrying the
earliest available sequence.

The cursor rule from design document §9.2 lives in one pure function so the unit
test in Task 3's style can cover it:

```python
def cursor_is_stale(after: int, earliest: int | None, next_sequence: int) -> bool:
    return after + 1 < (earliest if earliest is not None else next_sequence)
```

- [ ] **Step 3: Implement the route**

`GET /api/v1/runs/{run_id}/events` authenticates from the cookie, reads
`workspace_id` from the query, and resolves the cursor from `Last-Event-ID`
first and `last_event_id` second. It verifies membership and ownership through
`RunCoordination` before opening the stream, so a refusal is a normal Problem
Details response rather than a half-open stream.

The generator loop uses a fresh short-lived session per poll, sends a comment
heartbeat every `sse_heartbeat_seconds`, waits on the notifier between polls so
Redis only shortens latency, and returns when the Run is terminal and its final
event has been delivered. Add a module docstring explaining the two deliberate
deviations from the header convention and why `EventSource` forces them.

- [ ] **Step 4: Verify and commit**

```powershell
uv run --no-sync pytest packages/backend/tests/integration/runs/test_run_events_sse.py -v
git add packages/backend/src/tiny_hermes/runs packages/backend/src/tiny_hermes/api/app.py packages/backend/tests/integration/runs/test_run_events_sse.py
git commit -m "feat: stream run events with resumable cursors"
```

### Task 9: The three-process flow

**Files:**
- Create: `packages/backend/tests/integration/runs/test_execution_flow.py`

- [ ] **Step 1: Write the failing end-to-end flow**

One test drives the API, a `WorkerRuntime`, and a `SchedulerRuntime` against one
PostgreSQL and one Redis, using only public routes plus the two runtimes:

1. bootstrap, log in, create a workspace, publish a `continue_once` Agent;
2. create one Session and submit three Runs with three keys;
3. subscribe to SSE for all three;
4. drive `run_once()` until the Session drains, asserting that at every moment at
   most one Run is `running`, that Runs finish in `session_sequence` order, and
   that a pending Run never acquires a lease;
5. assert each Run's SSE transcript is contiguous from `run_created` to its
   terminal event, with no duplicate and no gap;
6. assert the audit trail contains one success record per write.

- [ ] **Step 2: Add the restart scenario to the same file**

Claim a Run with a one-second lease, abandon it without recording a slice, let
the lease lapse, run the Scheduler, and assert the Run becomes `interrupted`
then `queued`. Then let a second `WorkerRuntime` with a different `worker_id`
finish it, and assert:

- no event sequence was skipped or duplicated;
- `consumed_execution_ms` counts only recorded slices, not the abandoned one;
- the Session head never pointed at a terminal Run;
- the original idempotency record still replays the original creation snapshot.

- [ ] **Step 3: Verify and commit**

Run the flow ten times to catch ordering flakiness, then the whole suite.

```powershell
1..10 | ForEach-Object { uv run --no-sync pytest packages/backend/tests/integration/runs/test_execution_flow.py -q }
git add packages/backend/tests/integration/runs/test_execution_flow.py
git commit -m "test: prove the three process run flow"
```

### Task 10: Compose, CI, docs, and the exit record

**Files:**
- Modify: `deploy/compose/compose.yaml`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/development.md`
- Create: `docs/superpowers/verification/2026-08-10-m1-run-execution.md`

- [ ] **Step 1: Add the two Compose services**

Add `worker` and `scheduler` reusing the existing `apps/api/Dockerfile` image and
the shared `*app-env` anchor, each depending on `migrate` completing
successfully and on `postgres` and `redis` being healthy. Neither publishes a
port. Give each a liveness command that exits non-zero when the process is gone,
rather than inventing an HTTP endpoint they do not serve.

Bring the stack up from empty volumes and confirm all seven services reach a
healthy or successfully-completed state. Resolve and report the exact
project-owned volume names before removing anything, and do not delete a volume
that is not confirmed to belong to this project.

- [ ] **Step 2: Extend CI**

Add a `redis` service to the `backend-integration` job, since the wake-up tests
now need one, and keep the existing PostgreSQL service and migration round trip.
Extend the concurrency repeat loop with the new focused files:

```yaml
- name: Repeat concurrency regressions
  run: |
    for attempt in $(seq 1 10); do
      uv run pytest \
        packages/backend/tests/integration/runs/test_run_creation.py \
        packages/backend/tests/integration/runs/test_run_control.py \
        packages/backend/tests/integration/runs/test_run_coordination.py \
        packages/backend/tests/integration/runs/test_run_retry.py \
        packages/backend/tests/integration/runs/test_scheduler.py \
        packages/backend/tests/integration/runs/test_execution_flow.py -q
    done
```

Add one job step that runs the whole integration suite with `REDIS_URL` pointed
at an unused port, proving the platform degrades to polling rather than failing.

- [ ] **Step 3: Update development documentation**

Replace the phase-2A sentence stating that Runs stay `queued`. Document how to
run the Worker and Scheduler locally, how to pick a scenario when publishing an
Agent, and how to subscribe to the event stream with `curl -N`. State plainly
which capabilities still do not exist: real models, tools, files, sandbox, and
every phase-2C page.

- [ ] **Step 4: Run the complete fresh verification**

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
uv run --no-sync alembic downgrade 20260810_0002
uv run --no-sync alembic upgrade head
uv run --no-sync alembic downgrade base
uv run --no-sync alembic upgrade head

corepack pnpm install --frozen-lockfile
corepack pnpm --filter @tiny-hermes/web lint
corepack pnpm --filter @tiny-hermes/web test
corepack pnpm --filter @tiny-hermes/web build
docker compose -f deploy/compose/compose.yaml up -d --build --wait
docker compose -f deploy/compose/compose.yaml ps -a
```

Run tracked-file secret scanning with secret-shaped minimum lengths and
`git diff --check`.

- [ ] **Step 5: Write the verification record**

Follow the phase-2A record's structure: commits, versions, commands and counts,
concurrency repetitions, migration round trip, deliberate deviations, known
phase-2B limits, and redacted failure evidence. Do not copy cookies, passwords,
bootstrap tokens, request bodies, or database URLs.

- [ ] **Step 6: Commit the verified slice**

```powershell
git add deploy/compose/compose.yaml .github/workflows/ci.yml docs/development.md docs/superpowers/verification/2026-08-10-m1-run-execution.md
git commit -m "ci: verify run execution processes"
git status --short
```

Expected: commit succeeds and status is clean.

## 3. Phase 2B completion checklist

- [ ] A Run created through the public API reaches a terminal state with no
      manual signal.
- [ ] `apply_signal` is called by production code only; no test-only route
      exists.
- [ ] The deterministic provider is selected by a validated Agent Version
      policy, not by test configuration.
- [ ] An execution slice boundary returns the Run to `queued`, releases the
      lease, and keeps it Head.
- [ ] A running pause becomes `paused(manual)` only at the next checkpoint.
- [ ] An exhausted root budget produces `paused(limit)` and `run_limit_reached`
      without another model call.
- [ ] Execution milliseconds accumulate only for recorded slices and never
      double count.
- [ ] An abandoned lease becomes `interrupted` and then `queued`, bounded by
      `max_recovery_attempts`.
- [ ] Concurrent API, Worker, and Scheduler event writes stay unique and
      contiguous.
- [ ] Scheduler scans are idempotent and audited only when they change data.
- [ ] SSE resumes from `Last-Event-ID` with no gap and returns `410` with a
      usable resynchronization hint.
- [ ] The whole suite passes with Redis unreachable.
- [ ] Migration passes upgrade, `alembic check`, downgrade to `20260810_0002`,
      downgrade to base, and upgrade again.
- [ ] Compose starts all seven services from empty volumes.
- [ ] No tool, file, sandbox, model-endpoint, Chat Completions, or UI
      placeholder is represented as working before it exists.
