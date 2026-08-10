# M1 Run Execution Design

> Date: 2026-08-10
>
> Status: written design for user review
>
> Delivery slice: M1 phase 2B of three

## 1. Purpose and authority

Phase 2A fixed the Agent, Session, and Run rules and their PostgreSQL
transaction guarantees. Every Run it accepts stays `queued` forever, because
nothing executes them.

This slice adds the two long-running processes that make a Run move, the
deterministic model substitute they call, the Redis wake-up that shortens their
latency, and the SSE stream that lets a client watch a Run without polling.

The following documents remain authoritative:

- `docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` v2.4;
- `docs/superpowers/specs/2026-08-09-tiny-hermes-m1-technical-design.md` v1.1;
- `docs/superpowers/plans/2026-08-10-tiny-hermes-m1-roadmap.md` phase two;
- `docs/superpowers/specs/2026-08-10-m1-run-foundation-design.md` for the seams
  this slice consumes.

This document does not weaken or replace them, and it does not redesign anything
phase 2A already proved. It consumes `RunStore` exactly as committed.

## 2. Observable outcome

After phase 2B, an authenticated developer can:

1. publish an Agent whose model policy selects a deterministic scenario;
2. create a Session and submit a Run as in phase 2A;
3. watch that Run leave `queued`, reach `running`, and finish, without any
   manual signal;
4. subscribe to `GET /api/v1/runs/{run_id}/events`, disconnect, reconnect with
   `Last-Event-ID`, and receive every committed event exactly once with no gap;
5. receive `410 Gone` with the earliest available sequence and the Run snapshot
   URL when the cursor is older than the retained events;
6. pause a running Run and observe it become `paused(manual)` at the next safe
   checkpoint rather than immediately;
7. retry a genuinely failed Run, because `fail_replay_safe` produces a real
   replay-safe failure instead of a test-only signal;
8. kill the Worker mid-slice and see the Run return to `queued` within the lease
   window instead of being stuck in `running`.

Every one of these is observable through the public HTTP surface plus process
control. No test-only route is added.

## 3. Explicit non-goals

Phase 2B does not deliver:

- real model endpoints, provider SDKs, or `SafeOutboundClient`;
- tools, files, SessionWorkspace, WorkspaceRevision, Artifacts, or any sandbox;
- Sandbox Controller, freeze/thaw, or sandbox reservation;
- approval routes, governance decisions, or sub-agent and child Run waiting;
- ServiceAccounts, API keys, EndUsers, Chat Completions, or Feishu;
- Agent Builder, Playground, or Run Detail pages;
- context compression, memory, skills, or an independent Goal judge.

Phase 3 adds the real model, tools, and sandbox. Phase 2C adds the minimum
pages, browser acceptance, and operator documentation. States that only those
phases can reach — `waiting_approval` and `waiting_external` — keep their
existing state-machine transitions and remain reachable only through
`ApplySignalCommand`. No route, timer, or placeholder pretends to produce them.

## 4. Processes and seams

### 4.1 Three processes, one release unit

| Process | Phase-2B responsibility | Never does |
|---|---|---|
| `api` | Everything from phase 2A, plus the SSE event stream and publishing wake-ups after commit | Claim a lease, call a model, decide a Run state |
| `worker` | Claim one Head Run lease, run one execution slice against the deterministic model, checkpoint, release | Bypass `RunStateMachine`, write state columns, choose event sequences |
| `scheduler` | Reclaim expired leases, recover safe interrupted Runs, repair Session heads, expire idempotency records, prune retained events | Execute a Run, act as the queue truth |

All three import the same `tiny_hermes` package and the same application
interfaces. `worker` and `scheduler` get their own console scripts and their own
Compose services. Neither embeds a copy of a state decision.

### 4.2 What the Worker is allowed to call

The Worker never touches a table. Its whole vocabulary is the phase-2A store
interface plus one new command:

- `claim_head(ClaimRunCommand)` — already committed and already race-proof;
- `renew_lease(RenewLeaseCommand)` — new, described in §6.2;
- `apply_signal(ApplySignalCommand)` — every state change;
- `append_events(AppendEventsCommand)` — every event;
- `record_slice(RecordSliceCommand)` — new, described in §7.3, which persists a
  checkpoint, accumulates budget consumption, and releases the lease in one
  transaction.

`record_slice` exists because a checkpoint, a budget increment, a lease release,
and a state signal are one business operation. Splitting them across calls would
let a crash leave a released lease with an unaccounted execution segment.

### 4.3 Model substitute behind a port

`ModelProvider` is a port in `runs/ports/model.py`:

```python
class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
```

`DeterministicModelProvider` is the only phase-2B adapter. It reads the
`scenario` already validated into every Agent Version and returns a fixed
result after a configurable delay (default 50 ms, matching the reference
environment in product design v2.4 §21):

| Scenario | Round 1 | Round 2 |
|---|---|---|
| `complete` | final text, `stop_reason=completed` | not reached |
| `fail_replay_safe` | `stop_reason=failed`, replay-safe checkpoint, no external effect | not reached |
| `continue_once` | `stop_reason=continue`, replay-safe checkpoint | final text, `stop_reason=completed` |

The adapter performs no network call and needs no outbound policy. Phase 3
replaces it with a real provider behind the same port and adds
`SafeOutboundClient` at that time.

## 5. Data model

Phase 2B adds no new table. It adds one additive migration
`20260810_0003_run_execution.py` with two columns and one widened constraint:

| Change | Purpose |
|---|---|
| `runs.recovery_attempts` | New column, non-null, default 0. Counts automatic interrupted-to-queued recoveries so a poisoned Run cannot loop forever. |
| `runs.last_heartbeat_at` | New column, nullable. The most recent lease renewal, so the Scheduler can distinguish a slow slice from an abandoned one in diagnostics. |
| `ck_run_events_event_type` | Dropped and recreated with `run_limit_reached` added. Phase 2A derived the event vocabulary mechanically from `RunSignal` plus creation, retry, and repair; the safety-valve event required by product design v2.4 §12.3 is the first name that is not signal-derived, so `RunEventType` gains an explicit member and the constraint must be widened to match. |

The migration is still additive in effect: it adds columns with safe defaults
and only relaxes a check constraint. Downgrade drops the two columns and
restores the narrower constraint, which is safe because no phase-2A row can
carry the new event type.

Everything else this slice needs already exists: `worker_leases` carries the
owner, expiry, release time, and version; `run_budget_scopes` carries
`consumed_execution_ms`, `consumed_model_calls`, and `consumed_tokens`;
`runs.checkpoint`, `runs.checkpoint_replay_safe`, and
`runs.checkpoint_effect_status` carry recovery safety; `idempotency_records`
already carries `expires_at`.

If implementation uncovers a genuine third need, it goes into the same additive
migration and is called out in the plan rather than smuggled in.

## 6. Worker

### 6.1 Loop shape

```text
while running:
    run = claim_head(worker_id, lease_seconds)
    if run is None:
        await wake_up_or_timeout(idle_poll_seconds)
        continue
    await execute_slice(run)
```

`claim_head` is unchanged from phase 2A: queued, Session Head, no blocker, no
unexpired lease, `FOR UPDATE OF runs SKIP LOCKED`. Several Workers may run; the
existing race test already proves only one wins.

`wake_up_or_timeout` waits on Redis for at most `idle_poll_seconds` (default 2
seconds, range 1–30). A missed, delayed, or undelivered Redis message only costs
latency: the next poll finds the Run in PostgreSQL. **The Worker must pass its
whole test suite with Redis unreachable.**

### 6.2 Lease renewal

A lease lasts `lease_seconds` (default 30). While a slice is executing, a
renewal task extends it every `lease_seconds / 3`. `renew_lease` updates
`expires_at`, bumps `worker_leases.version`, and sets `runs.last_heartbeat_at`,
but only when the lease row still belongs to this worker with the expected
version. A renewal that matches nothing means the Scheduler already reclaimed
the Run; the Worker abandons the slice immediately without writing state, and
the Scheduler's `interrupted` decision stands.

### 6.3 Execution slice

One slice is one or more Goal rounds, bounded by `max_slice_seconds` (default
30, range 10–300). A round is one deterministic model call plus, in later
phases, its tool calls. Phase 2B has no tools, so a round is one model call.

After each round the Worker checks, in this order:

1. **Cancel requested.** `cancel_requested_at` set → `SAFE_CANCEL_STARTED` then,
   because phase 2B has nothing to clean up, `SAFE_CANCEL_FINISHED` in the same
   transaction. The Run becomes `cancelled` and the Session hands off.
2. **Pause requested.** `pause_requested_at` set → `SAFE_PAUSE_REACHED` with
   `manual`.
3. **Budget exhausted.** No remaining execution time, elapsed time, or model
   calls → `SAFE_PAUSE_REACHED` with `limit`, plus a `run_limit_reached` event.
   No new model call may start.
4. **Model said stop.** `completed` → `COMPLETED`; `failed` → `FAILED`.
5. **Slice expired.** Elapsed slice time at or beyond `max_slice_seconds` →
   `SLICE_ENDED`, back to `queued`, lease released, same Worker free to compete
   again.
6. Otherwise continue to the next round inside the same lease.

Checks 1 through 3 come before 4 so a user's pause or the safety valve wins over
a model that wants to keep going. Every one of these outcomes is applied through
`record_slice`, and every state value comes from `RunStateMachine`.

### 6.4 Budget accounting

The Worker measures only the wall time it actually held a valid lease in
`running`. That segment is added to `run_budget_scopes.consumed_execution_ms`
with the version predicate already used by retries, in the same transaction that
records the checkpoint. Queue time, pause time, and wait time never consume the
execution budget; `max_elapsed_seconds` covers those separately and is already
enforced by `BudgetSummary.allows_execution`.

`consumed_model_calls` increments once per model call. `consumed_tokens`
increments by the substitute's reported usage. `max_tokens` stays null, so token
usage is recorded but no strict ceiling is enforced, exactly as in phase 2A.

### 6.5 Shutdown

`SIGTERM` stops the claim loop and lets the current round finish to its next
checkpoint, up to a shutdown grace period. The slice then ends as `SLICE_ENDED`
and the lease is released, so a rolling restart returns Runs to `queued`
cleanly. If the grace period expires first, the Worker exits without writing;
the lease lapses and the Scheduler handles it as §7.1 describes. A Worker never
marks its own Run `interrupted` on the way out — it either finished a safe
checkpoint or it did not.

## 7. Scheduler

### 7.1 Expired leases

Runs in `running` or `cancelling` whose lease is unreleased and expired become
`interrupted` through `apply_signal`, with the reason recorded in the event
payload. There is no sandbox to quarantine in this slice, so the product's
sandbox-stop precondition is vacuously satisfied and is re-stated in code as a
comment rather than a fake check.

### 7.2 Interrupted recovery

An `interrupted` Run returns to `queued` through `RECOVERY_APPROVED` only when
all of the following hold:

- the last checkpoint is `checkpoint_replay_safe`;
- `checkpoint_effect_status` is not `unknown`;
- the root budget still allows execution;
- `recovery_attempts` is below `max_recovery_attempts` (default 3).

Each recovery increments `recovery_attempts`. When any condition fails, the Run
takes `RECOVERY_FAILED` and becomes `failed` with a reason in the event payload,
where the existing retry rules can still evaluate it. A Run never oscillates
between `interrupted` and `queued` unboundedly.

### 7.3 Other scans

| Scan | Action |
|---|---|
| Session head invariants | Calls the phase-2A `repair_session_head` for Sessions whose head is terminal, null with pending Runs, not the smallest non-terminal sequence, or whose pending blockers are wrong. Already idempotent and already audited only when it changes data. |
| Idempotency expiry | Deletes records whose `expires_at` has passed. Phase 2A set the expiry; this slice is what finally removes them. |
| Event retention | Deletes `run_events` older than `event_retention_hours` (default 168) belonging to terminal Runs only. This is what makes the SSE `410` path real rather than unreachable. |
| Wait deadlines | `wait_deadline_at` in the past on a `waiting_external` Run applies `EXTERNAL_PAUSED` with `external_timeout`. Phase 2B never produces such a Run, so this scan is written, tested through the signal seam, and honestly documented as dormant until phase 3. |

Compatibility-timeout cleanup is not implemented, because Chat Completions does
not exist until phase 4.

### 7.4 One scanner at a time

Multiple Scheduler replicas contend for a PostgreSQL advisory lock per scan
family. The loser skips that cycle rather than blocking. Individual repairs
still use row locks and state versions, so the advisory lock is an efficiency
measure, never the correctness argument. This is the wrapper the phase-2A
`repair_session_head` comment anticipated.

## 8. Redis wake-up

Redis carries notifications only. It is never read to decide what is true.

- The API publishes after a Run-accepting transaction commits, never inside it.
- The Worker publishes after a slice returns a Run to `queued`.
- The Scheduler publishes after a head handoff or a recovery makes a Run
  claimable.
- Payloads carry a workspace ID and a Run ID and nothing else. No message, no
  personality text, no checkpoint content.

A publish failure is logged and swallowed: the business transaction already
committed, and losing a notification only delays the next poll. Connection loss,
an empty channel, and a completely unavailable Redis are all covered by tests
that assert the Run still completes.

## 9. SSE event stream

### 9.1 Route

```text
GET /api/v1/runs/{run_id}/events?workspace_id={uuid}&last_event_id={sequence}
```

Content type is `text/event-stream`. Each frame uses the RunEvent sequence as
its `id`, the event type as its `event`, and a redacted JSON payload as its
`data`. The stream sends a comment heartbeat every `sse_heartbeat_seconds`
(default 15) so idle connections and proxies stay honest.

The workspace is a query parameter rather than the usual `X-Workspace-Id`
header, and the cursor is accepted as a query parameter in addition to the
standard `Last-Event-ID` header. This is a deliberate deviation: a browser
`EventSource` cannot set request headers. The session cookie still
authenticates, membership and ownership are still verified inside the
application transaction, and a cross-workspace identifier still returns a
generic `404`. `Last-Event-ID` wins when both are present, because that is what
a reconnecting `EventSource` sends automatically.

### 9.2 Continuity and 410

A cursor of `N` means "everything after sequence `N`". Let
`earliest = min(sequence)` over the Run's retained events, or
`runs.next_event_sequence` when none remain.

- `N + 1 >= earliest` — the stream resumes with no gap.
- `N + 1 < earliest` — the response is `410 Gone` with a Problem Details body
  carrying `earliest_available_sequence`, the Run snapshot URL, and the
  instruction to re-read the snapshot before resubscribing.

The server never emits a stream that looks contiguous while missing events in
the middle. The stream ends cleanly once the Run is terminal and its last event
has been delivered.

### 9.3 Reading

The stream reads committed rows from PostgreSQL. A Redis notification only
shortens the poll interval; correctness never depends on it. Each poll uses its
own short-lived session so a long-lived subscriber cannot pin one database
connection in an open transaction.

## 10. Configuration

| Setting | Default | Range |
|---|---|---|
| `worker_id` | generated per process | — |
| `worker_lease_seconds` | 30 | 10–300 |
| `worker_max_slice_seconds` | 30 | 10–300 |
| `worker_idle_poll_seconds` | 2 | 1–30 |
| `worker_shutdown_grace_seconds` | 20 | 5–120 |
| `scheduler_interval_seconds` | 5 | 1–60 |
| `max_recovery_attempts` | 3 | 0–10 |
| `event_retention_hours` | 168 | 1–8760 |
| `sse_heartbeat_seconds` | 15 | 5–60 |
| `deterministic_model_delay_ms` | 50 | 0–5000 |

All are validated in `Settings` with the same explicit bounds style already used
for `session_ttl_seconds`. Integration tests override them rather than sleeping
for production durations.

One runtime dependency is added: the `redis` package for its asyncio client.
`redis_url` is already in `Settings` and already wired into Compose, so no new
service or environment variable appears. `uv.lock` changes in the same commit
that adds the dependency, and CI keeps using `uv sync --frozen`.

## 11. Failure and transaction behavior

- Everything phase 2A established still holds: one business operation is one
  transaction, the state machine is the only decider, denied controls are
  audited, and stale versions never last-write-wins.
- A Worker that loses its lease writes nothing. The Scheduler's decision is
  authoritative.
- A crash between the model call and the checkpoint commit loses the round, not
  the Run: the lease lapses, the Run becomes `interrupted`, and recovery replays
  from the last committed checkpoint. The deterministic substitute has no
  external effect, so replay is always safe in this slice; the
  `checkpoint_effect_status` machinery is still written and honored so phase 3
  can set `unknown` truthfully.
- Redis being down degrades latency only.
- An SSE subscriber disconnecting must not leave an open transaction, an
  unreleased connection, or a partially written event.

## 12. Verification strategy

### 12.1 Fast domain tests

- the deterministic provider's three scenarios and their stop reasons;
- the slice-boundary decision order in §6.3, as a table-driven test over
  cancel-requested, pause-requested, budget-exhausted, model-stop, and
  slice-expired, asserting the correct signal for every combination;
- budget accumulation arithmetic, including that queue time is never counted;
- the SSE cursor rule in §9.2, including the empty-retention case.

### 12.2 PostgreSQL and process integration tests

- a published `complete` Agent's Run reaches `completed` with no manual signal,
  and its events are contiguous from `run_created` to `run_completed`;
- a `continue_once` Run crosses a slice boundary: it returns to `queued`, its
  lease is released, it is claimed again, and it finishes — proving the Head
  invariant survives re-queueing;
- a `fail_replay_safe` Run genuinely fails, and the phase-2A retry route then
  derives a real retry that itself executes;
- pausing a `running` Run records the request and only becomes `paused(manual)`
  at the next checkpoint;
- a Run whose root budget is exhausted becomes `paused(limit)` and writes
  `run_limit_reached` without starting another model call;
- killing a Worker mid-slice leaves the lease to expire, the Scheduler moves the
  Run to `interrupted` and then back to `queued`, and a second Worker finishes
  it — with no duplicate events and no lost committed state;
- a poisoned Run stops at `max_recovery_attempts` and becomes `failed`;
- the Scheduler is idempotent: a second immediate cycle changes nothing and
  writes no audit;
- two Scheduler replicas under the advisory lock produce one repair, not two;
- SSE delivers events, survives a disconnect and `Last-Event-ID` reconnect with
  no gap and no duplicate, and returns `410` with the earliest available
  sequence after retention pruning;
- the whole suite passes with Redis stopped.

### 12.3 Process-level flow

One flow starts the API, a Worker, and a Scheduler against one PostgreSQL and
one Redis, publishes an Agent, submits three Runs into one Session, subscribes
to SSE for each, and asserts they execute strictly in FIFO order with one Head
at a time. It then restarts the Worker mid-flight and asserts no committed state
was lost and no event sequence was skipped.

No test-only route, fake successful response, or in-memory substitute is used
for this flow. The deterministic model is not a test double — it is the shipped
phase-2B provider, selected by a validated Agent Version policy.

## 13. Exit criteria and next seams

Phase 2B is ready to merge only when:

- every phase-1 and phase-2A check still passes unchanged;
- a Run created through the public API reaches a terminal state with no manual
  signal, and `apply_signal` is used by production code only;
- concurrent API, Worker, and Scheduler event writes still produce unique
  contiguous sequences;
- a killed Worker's Run returns to `queued` within the lease window, and a
  Redis outage costs latency only;
- SSE resumes from `Last-Event-ID` with no gap and returns `410` with a usable
  resynchronization hint when the cursor is too old;
- the additive migration passes upgrade, `alembic check`, downgrade, and upgrade
  again;
- Compose starts `api`, `worker`, `scheduler`, `postgres`, `redis`, `minio`, and
  `web` from empty volumes and reaches a healthy state;
- the repository still contains no tool, file, sandbox, model-endpoint, or UI
  placeholder pretending to be operational.

Phase 2C consumes the stable Run snapshots and this event stream to build the
minimum Agent draft, publish, Run list, and event viewer pages, plus the browser
acceptance and restart scenarios. Phase 3 replaces `DeterministicModelProvider`
behind the same port, adds `SafeOutboundClient`, and fills the sandbox
preconditions that §7.1 currently satisfies vacuously.
