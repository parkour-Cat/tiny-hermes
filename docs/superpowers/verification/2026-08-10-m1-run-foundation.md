# M1 Run Foundation Verification Record

> Date: 2026-08-10
>
> Slice: M1 phase 2A of three
>
> Branch: `codex/m1-run-foundation` in `.worktrees/codex-m1-run-foundation`
>
> Verified at commit: `b5a0a5189948cd1aef724f693f049034d1a3d0cb`
> (this record and the CI, docs, and HTTP tracer changes commit on top of it)

## 1. Commits in the slice

| Commit | Subject |
|---|---|
| `28f3e06` | feat: define immutable agent version schema |
| `bb36a8f` | feat: add agent catalog rules |
| `81886cd` | feat: define authoritative run state machine |
| `f39d3cb` | feat: add agent and run database schema |
| `da63cbd` | feat: expose agent publication workflow |
| `d2a5e5b` | feat: accept idempotent fifo runs |
| `13f64c2` | feat: control runs with atomic events |
| `928fb35` | feat: add run claim and head repair seam |
| `b5a0a51` | feat: derive retries from shared budgets |

## 2. Environment

| Component | Version |
|---|---|
| Python | 3.12.6 |
| uv | 0.11.26 |
| FastAPI | 0.141.1 |
| Pydantic | 2.13.4 |
| SQLAlchemy | 2.0.51 |
| Alembic | 1.19.1 |
| PostgreSQL | 17.6 (Compose `postgres` service) |
| Node | 24.6.0 |
| pnpm | 10.15.0 |

Every database command targeted `tiny_hermes_test` only. Database URLs, cookies,
CSRF tokens, bootstrap tokens, passwords, and Agent personality text are
deliberately absent from this record.

## 3. Commands and results

### 3.1 Static checks and fast domain tests

```text
uv sync --frozen                                        Checked 47 packages
uv run --no-sync ruff check packages/backend migrations All checks passed!
uv run --no-sync pyright                                0 errors, 0 warnings
uv run --no-sync pytest packages/backend/tests/unit -q  61 passed
```

### 3.2 PostgreSQL integration tests

```text
uv run --no-sync alembic upgrade head                          ok
uv run --no-sync pytest packages/backend/tests/integration -q  56 passed
uv run --no-sync alembic check                 No new upgrade operations detected
uv run --no-sync alembic downgrade 20260810_0001               ok
uv run --no-sync alembic upgrade head                          ok
uv run --no-sync alembic downgrade base                        ok
uv run --no-sync alembic upgrade head          alembic current: 20260810_0002 (head)
```

Total: **117 tests passing** (61 unit, 56 integration).

Migration round trip was checked twice: to `20260810_0001` (the ten new tables
disappear, the six phase-one tables and `alembic_version` remain) and to `base`.
After the partial downgrade the remaining public tables were exactly
`alembic_version, audit_events, auth_identities, auth_sessions, memberships,
users, workspaces`, and the phase-one integration tests still passed against
that state.

The two circular ownership pointers are created by explicit `ALTER TABLE` after
all tables exist and dropped first on the way down. Their presence was confirmed
directly in `pg_constraint`:

```text
fk_agents_current_version  agents   -> agent_versions
fk_sessions_head_run       sessions -> runs
```

### 3.3 Concurrency repetitions

Each focused file was run as ten separate pytest processes, all green:

| File | Iterations | Result |
|---|---|---|
| `test_run_creation.py` | 10 | 8 passed each |
| `test_run_coordination.py` | 10 | 8 passed each |
| `test_run_retry.py` | 10 | 13 passed each |
| `test_run_control.py` | 5 (with the rest of `runs/`) | 18 passed each |

CI repeats all four files ten times in `backend-integration`.

### 3.4 Web checks

```text
corepack pnpm install --frozen-lockfile   Already up to date
eslint . --max-warnings 0                 clean
vitest                                    1 file, 1 test passed
vite build                                built in 8.73s
```

The root `web:lint` / `web:test` / `web:build` scripts shell out to a bare
`pnpm`, which is not on `PATH` in a Git Bash session that only has Corepack. The
same three commands were run through `corepack pnpm --filter @tiny-hermes/web`.
CI enables Corepack and puts `pnpm` on `PATH`, so the root scripts work there.

### 3.5 Compose readiness

```text
docker compose -f deploy/compose/compose.yaml up -d --build --wait
docker compose -f deploy/compose/compose.yaml ps -a
```

```text
api        Up (healthy)
migrate    Exited (0)
minio      Up (healthy)
postgres   Up (healthy)
redis      Up (healthy)
web        Up (healthy)
```

No Docker volume was reset or deleted during this verification.

### 3.6 Secret scanning

`git diff --check` reported nothing. A tracked-file scan for secret-shaped
values (AWS keys, GitHub tokens, `sk-` keys, Slack tokens, PEM private keys,
JWTs) returned no matches. A second scan for assignment-shaped
`password|secret|token|api_key|private_key` values of at least twenty characters
returned only self-describing placeholders in `docs/development.md`, the phase-one
plan, and test settings (for example `test-cookie-secret-with-32-characters`).
The only tracked environment file is `.env.example`.

## 4. What the slice proves

- Agent Drafts carry a positive revision; a stale `expected_revision` returns
  `409 draft_revision_conflict` and never overwrites.
- Publishing an unchanged Draft returns the current Agent Version with `200` and
  allocates no new version number; changed content allocates `max + 1` under an
  Agent row lock. Rollback moves only the pointer and keeps both versions.
- Three Runs in one Session take sequences 1, 2, 3; Run 1 is Head with no
  blocker and Runs 2 and 3 point at it. Each root Run owns one budget row,
  points `budget_root_run_id` at itself, and has exactly one `run_created` event
  at sequence 1 with `next_event_sequence = 2`.
- Two genuinely overlapping `POST /api/v1/runs` calls with one key produce one
  `201` and one `200 Idempotent-Replayed: true` with matching IDs, and increase
  message, Run, budget, event, idempotency, and audit-success counts by exactly
  one each. The same key with a different body returns
  `409 idempotency_key_reused` and writes nothing.
- Three independent database sessions each reserving two events produce exactly
  `[2, 3, 4, 5, 6, 7]` with `next_event_sequence = 8`.
- `RunStateMachine` is the only code that chooses a Run state. A table-driven
  test walks all 190 state/signal pairs and asserts each is either a documented
  transition, one of the two running request-only cases, or rejected.
- Controls compare `state_version` and increment it once; a stale version
  returns `409 state_version_conflict`; an illegal control writes a redacted
  `run.control_denied` audit that survives the refusal and changes no state.
- Terminalizing Run 1 while Run 2 is already cancelled hands the Session
  directly to Run 3, clears its blocker, and leaves Run 2 cancelled. A terminal
  pending Run does not move the head.
- Two barriered claimers of the same queued Head Run yield exactly one
  `ClaimedRun`, one unreleased lease, one `run_lease_acquired` event, and
  `state_version = 2`.
- Head repair fixes a terminal head, a null head, a too-late head, and wrong
  pending blockers; a second call changes nothing and writes no audit or event.
  An all-terminal Session is audited but writes no Run Event.
- Derived Retries share one root budget row: a sequential chain of two keeps one
  budget with count 2, five concurrent retries for the last slot produce exactly
  one `201` and four `409 retry_limit_reached`, replaying the winning key returns
  the same Run without incrementing, and a fourth sequential derivation is
  refused.
- Selecting a workspace never substitutes for membership: a caller with no
  membership gets `403 forbidden` before any lookup, and a caller authorized in
  both workspaces gets a generic `404` carrying no Agent name, alias,
  personality, or content hash.

## 5. Deliberate deviations from the plan

1. **`AgentCatalog.publish` returns `PublishResult`, not `AgentVersion`.** The
   route needs the `unchanged` flag to choose `200` versus `201`; returning the
   bare version would force a second read. The plan's unit test was adapted to
   `first.version.id` and additionally asserts the flag.
2. **Test packages gained `__init__.py` files.** The plan's Task 2 test uses
   `from .test_agent_models import valid_spec`, which needs a package under
   pytest's default import mode.
3. **Concurrency tests use `asyncio.gather`, not threads.** `TestClient` is
   synchronous, so overlapping HTTP requests use `httpx2.AsyncClient` over
   `ASGITransport`, and store-level races use independent `async_sessionmaker`
   sessions, matching the existing `test_bootstrap_concurrency.py` pattern.
4. **Retry eligibility reports root-budget limits before Session position.**
   The plan expected four concurrent losers to see `retry_limit_reached`. With
   the latest-Run rule checked first they would instead see
   `retry_context_stale`, because the winner's new Run also makes the source no
   longer latest. The shared allowance is the durable, actionable reason, so it
   is reported first; the standalone stale-source case still returns
   `retry_context_stale`.
5. **No in-adapter retry loop for event-sequence conflicts.** The allocator is a
   single `UPDATE ... RETURNING` that hands out disjoint ranges, so a duplicate
   `(run_id, sequence)` cannot arise from this seam. A unique violation would
   poison the surrounding transaction, making a three-attempt retry inside the
   same session incorrect. The violation is detected and surfaced as
   `event_sequence_conflict` instead; a real retry belongs at a
   transaction-per-attempt boundary that phase 2B can add with the Worker loop.
6. **The Task 10 HTTP tracer passed on its first run.** It exercises behavior
   already built and committed in Tasks 1 through 9, so there was no failing
   state to observe first.

## 6. Known phase-2A limits

- **Runs never leave `queued` on their own.** There is no Worker, Scheduler,
  Redis wake-up, SSE stream, deterministic model substitute, or sandbox. Tests
  reach `running`, `completed`, and `failed` only through
  `RunCoordination.apply_signal`, which is the seam phase 2B will drive.
- `claim_head` acquires a lease but never renews, expires, releases, or executes
  it. Expired leases are only filtered out of claim candidates.
- `repair_session_head` locks exactly one Session. It starts no loop and takes no
  global advisory lock; that wrapper is phase-2B Scheduler work.
- Idempotency records get `expires_at = finished_at + 24 hours` when their Run
  terminalizes, but nothing deletes expired rows yet. Phase 2A keeps them rather
  than adding a fake cleanup process.
- `max_tokens` is null for every phase-2A budget, meaning no strict total-token
  ceiling was requested. It is not treated as zero, and only a validated Agent
  Version policy will ever set one.
- Tools are rejected, the model provider is restricted to `deterministic`, and
  `workspace_revision_id` is null everywhere until phase-three file support.
- The Run Coordination adapter reads `agents` and `agent_versions` directly to
  fix an Agent Version onto a Run. Both modules ship in one release unit; if they
  ever split, that read needs its own port.
- No Agent Builder, Playground, or Run Detail page exists. The Web application
  still shows only login and the workspace console.

## 7. Redacted failure evidence

Three failures were found and fixed during implementation; none survive.

1. **Autogenerated migration silently dropped both circular foreign keys.**
   Alembic rendered `use_alter=True` constraints inline inside `create_table`,
   where SQLAlchemy's compiler omits them and nothing adds them back. Fixed by
   writing them as explicit `op.create_foreign_key` calls after all tables exist,
   with matching `op.drop_constraint` calls first in `downgrade`. Verified in
   `pg_constraint`.
2. **Budget and Session-head writes raced ahead of the Run insert.** The
   unit-of-work flushed `run_budget_scopes` before `runs`, producing
   `ForeignKeyViolationError` on `run_budget_scopes_root_run_id_fkey`. Fixed by
   flushing the Run row on its own before anything references it.
3. **Audited control denials rolled back.** The route converts application
   errors into Problem Details, so the request dependency saw an `AppError`
   rather than the `AuditedDenial` marker and rolled the audit away. Fixed by
   carrying an explicit `audited` flag on `AppError`; the dependency now commits
   that one case and still rolls back everything else.

## 8. Phase-2A completion checklist

- [x] Agent Draft revisions and immutable Agent Versions are enforced.
- [x] Repeated unchanged publish does not create duplicate versions.
- [x] Session Head and pending FIFO invariants hold under cancellation and
      terminal handoff.
- [x] Run creation is database-first idempotent under concurrent requests.
- [x] Run Event sequences are contiguous across concurrent writer identities.
- [x] The full v2.4 state matrix is represented only by `RunStateMachine`.
- [x] Controls use state versions and denied operations are audited.
- [x] Two claimers cannot obtain the same Run.
- [x] Head repair is idempotent and audited only when it changes data.
- [x] Derived Retries share one root budget and concurrent requests cannot push
      the default count above three.
- [x] Workspace selection never substitutes for membership and ownership
      verification.
- [x] All old and new tests, migrations, static checks, Web checks, Compose
      readiness, and secret scans pass.
- [x] No phase-2B or phase-2C capability is represented as working before it
      exists.
