# M1 Run Execution Verification Record

> Date: 2026-08-10
>
> Slice: M1 phase 2B of three
>
> Branch: `m1-run-execution` in `.worktrees/m1-run-execution`
>
> Verified at commit: `a624e23753136cc8453d20db9ce1988d8309cc7f`
> (this record and the Compose, CI, and documentation changes commit on top of it)

## 1. Commits in the slice

| Commit | Subject |
|---|---|
| `77805f0` | docs: define m1 run execution slice |
| `278a890` | docs: plan m1 run execution implementation |
| `52c3c91` | feat: add deterministic model provider |
| `178ba65` | feat: add run execution columns |
| `4b87b8a` | feat: decide execution slice boundaries |
| `471047e` | feat: record execution slices atomically |
| `6f216bc` | feat: execute runs in worker slices |
| `c3ff582` | feat: wake workers without trusting redis |
| `4474871` | feat: reclaim and repair runs on a schedule |
| `6bd5595` | feat: stream run events with resumable cursors |
| `a624e23` | test: prove the three process run flow |

## 2. Environment

| Component | Version |
|---|---|
| Python | 3.12.6 |
| uv | 0.11.26 |
| FastAPI | 0.141.1 |
| Pydantic | 2.13.4 |
| SQLAlchemy | 2.0.51 |
| Alembic | 1.19.1 |
| uvicorn | 0.52.1 |
| redis-py | 8.1.0 |
| httpx2 | 2.10.0 |
| pytest | 9.1.1 |
| PostgreSQL | 17.6 (Compose `postgres` service) |
| Redis | 8.2.1 (Compose `redis` service) |
| Node | 24.6.0 |
| pnpm | 10.15.0 |

Every database command targeted `tiny_hermes_test` only. Database URLs, cookies,
CSRF tokens, session tokens, bootstrap tokens, passwords, Run input, and Agent
personality text are deliberately absent from this record.

## 3. Commands and results

### 3.1 Static checks and fast domain tests

```text
uv sync --frozen                                        Installed 1 package
uv run --no-sync ruff check packages/backend migrations All checks passed!
uv run --no-sync pyright                                0 errors, 0 warnings
uv run --no-sync pytest packages/backend/tests/unit -q  159 passed
```

### 3.2 PostgreSQL integration tests

```text
uv run --no-sync alembic upgrade head                          ok
uv run --no-sync pytest packages/backend/tests/integration -q  110 passed
uv run --no-sync alembic check                 No new upgrade operations detected
```

Total: **269 tests passing** (159 unit, 110 integration).

### 3.3 The suite with the wake-up channel unreachable

Redis buys latency, never correctness, so the platform has to keep working when
the wake-up channel is gone. With `REDIS_URL` pointed at a port nothing listens
on:

```text
REDIS_URL=redis://127.0.0.1:6399/0
uv run --no-sync pytest packages/backend/tests/integration -q -rs
                                               105 passed, 5 skipped in 209.28s
```

The five skips are exactly the tests in `runs/test_wakeup.py` that assert the
optimization itself; they cannot pass without a channel and are reported as
skipped rather than quietly passing. Everything else passes untouched, on
polling alone. The same run took 209s against 95s with Redis reachable, which is
the polling cost made visible.

Before this slice, `redis_url` was hardcoded in the integration `conftest.py`, so
a `REDIS_URL` override would have changed nothing and the CI step would have been
vacuous. The fixture now reads the environment, which is what makes the degraded
run meaningful.

### 3.4 Migration round trip

```text
uv run --no-sync alembic downgrade 20260810_0002               ok
uv run --no-sync alembic upgrade head                          ok
uv run --no-sync alembic downgrade 20260810_0001               ok
uv run --no-sync alembic upgrade head                          ok
uv run --no-sync alembic downgrade base                        ok
uv run --no-sync alembic upgrade head    alembic current: 20260810_0003 (head)
```

`20260810_0003` adds execution columns with safe defaults, so the downgrade to
`20260810_0002` is the one that matters for an already-deployed database. It was
checked in both directions, along with the phase-2A boundary and a full teardown.

### 3.5 Concurrency repetitions

Each iteration is a separate pytest process over the six files that carry a race.
CI runs the same loop ten times in `backend-integration`:

| File | Tests per iteration |
|---|---|
| `test_run_creation.py` | 8 |
| `test_run_control.py` | 10 |
| `test_run_coordination.py` | 8 |
| `test_run_retry.py` | 13 |
| `test_scheduler.py` | 11 |
| `test_execution_flow.py` | 2 |

Ten iterations, all green, 52 tests each:

```text
iter 1: 52 passed in 47.69s      iter 6:  52 passed in 48.84s
iter 2: 52 passed in 45.38s      iter 7:  52 passed in 52.82s
iter 3: 52 passed in 48.44s      iter 8:  52 passed in 47.07s
iter 4: 52 passed in 46.91s      iter 9:  52 passed in 44.05s
iter 5: 52 passed in 50.29s      iter 10: 52 passed in 44.91s
```

The three-Run flow test was additionally run ten times on its own when it was
written, also all green.

### 3.6 Web checks

```text
corepack pnpm install --frozen-lockfile   Done in 10.2s
eslint . --max-warnings 0                 clean
vitest                                    1 file, 1 test passed
vite build                                built in 9.46s
```

The root `web:lint` / `web:test` / `web:build` scripts shell out to a bare
`pnpm`, which is not on `PATH` in a Git Bash session that only has Corepack, so
the three commands were run through `corepack pnpm --filter @tiny-hermes/web`.
CI enables Corepack and puts `pnpm` on `PATH`, so the root scripts work there.
This is unchanged from phase 2A.

### 3.7 Compose readiness

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
scheduler  Up (healthy)
web        Up (healthy)
worker     Up (healthy)
```

Earlier in this slice the same stack was brought up from genuinely empty volumes.
Before that reset the project's named volumes were resolved with
`docker compose config --volumes` and `docker volume ls`, confirmed to be exactly
`tiny-hermes_postgres-data` and `tiny-hermes_minio-data`, reported, and only then
removed with a project-scoped `docker compose down -v --remove-orphans`. No
volume belonging to any other project was touched.

### 3.8 The deployed stack executes and streams

A Run was submitted to the containerized stack over plain HTTP and its event
stream read with `curl -N`, using only the session cookie and the `workspace_id`
query parameter the documentation shows:

```text
run created:              queued
stream ended on its own:  True
event types:              run_created, run_lease_acquired, run_completed
contiguous sequences:     True
```

No manual signal was sent. The containerized Worker claimed the Run through the
real Redis wake-up, executed both rounds of the `continue_once` scenario inside
one slice, and the stream closed itself once the Run was terminal.

### 3.9 Secret scanning

`git diff --check` reported nothing. A tracked-file scan for secret-shaped values
(AWS keys, GitHub tokens, `sk-` keys, Slack tokens, PEM private keys, JWTs)
returned no matches. The only tracked environment file is `.env.example`.

## 4. What the slice proves

- A Run created through the public API reaches a terminal state with no manual
  signal, driven only by the Worker and Scheduler an operator starts.
- `apply_signal` has no route. Grepping every `presentation` package finds no
  caller; the only production callers are the Worker runtime and the store's own
  internal reuse.
- The deterministic provider is chosen by the published Agent Version's
  `model_policy.scenario`, which the Agent Version schema validates. No test
  configuration selects it.
- Session serialization and lease exclusivity hold *while work is in flight*, not
  merely at rest. `ObservingModel` photographs `runs`, `worker_leases`, and
  `sessions` from a separate connection at the moment a model round is executing.
  Across six such photographs in the three-Run flow test, exactly one Run was
  `running`, the set of unreleased leases equalled the set of running Runs, and no
  Session head was already terminal.
- Three Runs in one Session finish in session order 1, 2, 3, all `completed`,
  under a Worker configured with a zero-second slice budget so every round ends at
  a boundary and the Run must be re-claimed.
- Every subscriber's transcript is contiguous from 1 with no gap and no repeat,
  and the count of `run_lease_acquired` frames across the three streams equals the
  count of `run.lease_acquired` audit rows — two independent records agreeing.
- A killed Worker is recovered without losing or repeating work. A Run claimed
  with a one-second lease and then abandoned is recovered by the Scheduler, and the
  survivor's transcript is `run_created`, `run_lease_acquired`, `run_interrupted`,
  `run_recovery_approved`, `run_lease_acquired`, `run_completed`.
- Execution milliseconds are charged for recorded slices, not possession. The
  abandoned Run was owned for over a second by a process that executed nothing;
  its shared budget recorded two model calls and well under one second.
- Replaying the original `Idempotency-Key` after recovery still returns `200` with
  `Idempotent-Replayed: true` and the original creation body.
- SSE resumes from `Last-Event-ID` with no gap, and a cursor older than the
  retained window returns `410` carrying `earliest_available_sequence` and a
  `run_url` to resynchronize from.
- Every audit row written across the flow has `result = "succeeded"` and a
  non-empty `request_id`.

## 5. Deliberate deviations from the plan

1. **The SSE tests run a real uvicorn server on a real socket, not `client.stream`.**
   Every ASGI test transport available here buffers the whole response body before
   returning it, which would make a streaming assertion meaningless and a
   mid-stream disconnect impossible to express. The `live_server`, `browser`,
   `events_url`, and `read_stream` fixtures live in the integration `conftest.py`
   so the flow test reuses the same real-HTTP path.
2. **The stream polls committed rows every 0.5s instead of subscribing to the
   wake-up notifier.** A Redis subscription is a per-connection resource, and the
   application's single shared subscription is not safe for concurrent readers.
   That is a real cost to pay for at most half a second of delivery latency, so
   the stream reads the database it must read anyway. This is recorded in the
   module docstring, not only here.
3. **The degraded CI run skips five tests rather than pretending they pass.** The
   tests in `runs/test_wakeup.py` that assert the wake-up optimization cannot be
   satisfied without a channel. They skip on an unreachable Redis; making them
   silently pass would have hidden the thing the step exists to prove.
4. **The Worker and Scheduler Compose services run the console script directly,
   not through `uv run`.** With `uv run` in between, PID 1 is a wrapper: SIGTERM
   never reaches the loop that knows how to finish its slice, and a liveness probe
   inspects the wrapper instead of the process. Running
   `/app/.venv/bin/tiny-hermes-worker` makes the process itself PID 1 and lets the
   probe read `/proc/1/cmdline`.
5. **The liveness probe reads `/proc/1/cmdline`, not `pgrep`.** A probe that
   searches all processes matches its own command line and would report healthy no
   matter what. Pinning it to PID 1 makes it a real question. It is still
   liveness, not progress: a Worker wedged inside a slice answers healthy, and the
   Scheduler's lease expiry is what actually recovers its Run.
6. **Compose has eight services, not the seven the plan anticipated.** The plan
   counted the long-running services; `migrate` is a one-shot that must exit `0`
   before the others start. Seven healthy plus one clean exit is the correct
   result.
7. **The three-process flow test passed on its first run.** It exercises behavior
   already built in Tasks 1 through 8, so there was no failing state to observe
   first. To confirm the assertions were not vacuous, the strictest one
   (`len(snapshot.running) == 1`) was deliberately mutated to `== 2`, the failure
   was confirmed to read real live UUIDs out of the database, and the mutation was
   reverted. The snapshot counts are pinned (`== 6`, `== 2`) so a silently
   non-executing test cannot pass.
8. **A `settings` pytest marker replaced per-test servers.** The heartbeat test
   needs a shorter `sse_heartbeat_seconds` than the rest; marking the test is
   cheaper and clearer than standing up a second live server inside it.
9. **`tests/integration/support.py` was added for two Protocols.** A
   `Callable[..., Awaitable[...]]` alias erases argument names and erases the
   `Coroutine` type `asyncio.create_task` requires, which produced 26 pyright
   strict errors. Naming the two callables as Protocols fixed all of them;
   `packages/backend/tests` was added to pyright's `extraPaths` so the import
   resolves.

## 6. Known phase-2B limits

- **There is no real model provider.** `DeterministicModelProvider` is the only
  one that exists. It performs no network call, so no outbound policy applies yet,
  and a Run's `input` is never sent anywhere.
- No tools, no file handling, and no sandbox. Tools are still rejected by the
  Agent Version schema.
- No Agent Builder, Playground, or Run Detail page. The Web application still
  shows only login and the workspace console, so every operation in
  `docs/development.md` is an API call.
- The liveness probe reports that the process exists, not that it is making
  progress. Progress is recovered by lease expiry, not by the probe.
- Idempotency records still get `expires_at` but nothing deletes expired rows.
- `workspace_revision_id` remains null everywhere until phase-three file support.
- **Carried over from phase 2A and found during this verification:** creating an
  Agent whose alias already exists in the workspace returns `500`, not the
  intended `409 agent_alias_taken`. `AgentAliasAlreadyUsed` is raised only by the
  in-memory adapter, so against PostgreSQL the `uq_agents_workspace_alias`
  violation escapes untranslated and the `409` branch in the routes is dead code.
  No integration test covers a duplicate alias. This is outside the phase-2B
  scope and is tracked separately rather than fixed here.

## 7. Redacted failure evidence

1. **An execution-time assertion was flaky under clock granularity.** The
   recovery test originally claimed a one-second lease and then expired it with a
   direct `UPDATE worker_leases SET expires_at = ...`, so no real time passed and
   the only remaining signal was two model delays of wall clock. One run in ten
   failed at `assert 92 >= (2 * 50)`, because integer truncation and timer
   granularity can undershoot a sum of sleeps. Fixed by making the test faithful
   to what it claims: the lease is now left to lapse on its own clock, the
   hand-written `UPDATE` was deleted, and the assertion became
   `0 < consumed_execution_ms < 1000` — the Run was possessed for over a second
   and billed well under it. Ten subsequent runs were clean.
2. **26 pyright strict errors from erased callable types.** Fixture-returned
   callables were annotated `Callable[..., Awaitable[...]]`, which loses argument
   names and is not the `Coroutine` type `asyncio.create_task` accepts. Fixed with
   the two Protocols in `tests/integration/support.py`.
3. **The first degraded run failed with 93 errors, and none were about Redis.**
   The volume reset earlier in this task had emptied `tiny_hermes_test`, so
   migration tests found no tables at all. Re-running `alembic upgrade head`
   against the test database restored it and the degraded run came back
   `105 passed, 5 skipped`. Recorded because the failure looked alarming and was
   purely an environment state, not a defect: a verification is only worth
   anything if the misleading intermediate results are reported too.

## 8. Phase-2B completion checklist

- [x] A Run created through the public API reaches a terminal state with no
      manual signal.
- [x] `apply_signal` is called by production code only; no test-only route
      exists.
- [x] The deterministic provider is selected by a validated Agent Version
      policy, not by test configuration.
- [x] An execution slice boundary returns the Run to `queued`, releases the
      lease, and keeps it Head.
- [x] A running pause becomes `paused(manual)` only at the next checkpoint.
- [x] An exhausted root budget produces `paused(limit)` and `run_limit_reached`
      without another model call.
- [x] Execution milliseconds accumulate only for recorded slices and never
      double count.
- [x] An abandoned lease becomes `interrupted` and then `queued`, bounded by
      `max_recovery_attempts`.
- [x] Concurrent API, Worker, and Scheduler event writes stay unique and
      contiguous.
- [x] Scheduler scans are idempotent and audited only when they change data.
- [x] SSE resumes from `Last-Event-ID` with no gap and returns `410` with a
      usable resynchronization hint.
- [x] The whole suite passes with Redis unreachable.
- [x] Migration passes upgrade, `alembic check`, downgrade to `20260810_0002`,
      downgrade to base, and upgrade again.
- [x] Compose starts all services from empty volumes.
- [x] No tool, file, sandbox, model-endpoint, Chat Completions, or UI
      placeholder is represented as working before it exists.
