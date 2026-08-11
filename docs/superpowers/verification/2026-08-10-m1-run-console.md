# M1 Run Console Verification Record

> Date: 2026-08-11
>
> Slice: M1 phase 2C of three
>
> Branch: `m1-run-execution` in `.worktrees/m1-run-execution`
>
> Verified at commit: `77ca8d2` (this record and the drill, CI, and
> documentation changes commit on top of it)

## 1. Commits in the slice

| Commit | Subject |
|---|---|
| `cda07e0` | fix: refuse a taken agent alias against PostgreSQL too |
| `72cf674` | docs: plan the phase-2C console file by file |
| `817b7c0` | feat: scope api requests to a workspace and fix csrf on put |
| `3552549` | feat: add the workspace-scoped console shell |
| `c83f5a2` | feat: list and create agents inside a workspace |
| `ba7878c` | feat: edit an agent draft with typed fields |
| `9cba4e4` | feat: publish an agent draft from the console |
| `84c5d44` | feat: list runs and submit one from the console |
| `eb83779` | feat: read the run event stream in the console |
| `e05e2da` | feat: add the Run detail page |
| `77ca8d2` | test: walk the console through the real stack |

The design document, `docs/superpowers/specs/2026-08-10-m1-run-console-design.md`,
was committed with `cda07e0`; the alias fix in that same commit closes the
phase-2A defect the previous record listed as carried over, because the console's
first completion checklist item asserts it.

## 2. Environment

| Component | Version |
|---|---|
| Python | 3.12.6 |
| uv | 0.11.26 |
| pytest | 9.1.1 |
| PostgreSQL | 17.6 (Compose `postgres` service) |
| Redis | 8.2.1 (Compose `redis` service) |
| Docker Engine | 29.5.3 |
| Node | 24.6.0 |
| pnpm | 10.15.0 |
| TypeScript | 6.0.3 |
| React | 19.2.8 |
| Ant Design | 6.5.4 |
| TanStack Query | 5.101.4 |
| react-router-dom | 7.18.2 |
| Vite | 8.2.1 |
| Vitest | 4.1.10 |
| msw | 2.15.0 |
| Playwright | 1.62 |

Every database command targeted `tiny_hermes_test` only. Database URLs, cookies,
CSRF tokens, session tokens, bootstrap tokens, passwords, Run input, and Agent
personality text are deliberately absent from this record, and from the drill's
output.

**The browser and drill runs used an isolated Compose project.** The everyday
`tiny-hermes` stack on this machine had been bootstrapped weeks earlier with an
account whose password is not recoverable, and the acceptance setup has to be
able to sign in. Rather than delete a volume to get a fresh platform, the same
`compose.yaml` was brought up a second time under `-p tiny-hermes-e2e`, which
gets its own volumes and leaves `tiny-hermes_postgres-data` untouched. Nothing in
this record required deleting any volume.

## 3. Commands and results

### 3.1 Backend, unchanged

```text
uv run --no-sync ruff check packages/backend migrations scripts  All checks passed!
uv run --no-sync pyright                                         0 errors, 0 warnings
uv run --no-sync alembic upgrade head                            ok
uv run --no-sync pytest packages/backend/tests -q                271 passed in 99.43s
```

271 = 159 unit + 112 integration. Phase 2B ended at 269; the two new integration
tests are the duplicate-alias pair from `cda07e0`.

### 3.2 Web checks

```text
corepack pnpm --filter @tiny-hermes/web lint   eslint . --max-warnings 0, no output
corepack pnpm --filter @tiny-hermes/web test   9 files, 62 tests passed in 84.40s
corepack pnpm --filter @tiny-hermes/web build  built in 10.25s
```

### 3.3 Browser acceptance against the Compose stack

```text
docker compose -f deploy/compose/compose.yaml up -d --build --wait   all services healthy
corepack pnpm exec playwright test --config tests/e2e/playwright.config.ts

  ok 1 [setup]      bootstrap the platform and sign in                            (1.5s)
  ok 2 [foundation] login, create two workspaces and logout                       (4.5s)
  ok 3 [console]    draft, publish, submit, watch, retry, and be refused a
                    foreign workspace                                            (16.0s)
  ok 4 [console]    the panes phase two cannot fill are absent, not empty         (7.2s)
  ok 5 [console]    an EventSource-shaped subscription is served, and an
                    unscoped one is refused                                       (2.8s)

  5 passed (35.4s)
```

Nothing is seeded and no route exists only for these tests. The Agent is created
and published through the forms, the Run is executed by the `worker` container,
and the timeline fills from the event stream the `api` container serves.

### 3.4 Restart drill

The stack was recreated with `DETERMINISTIC_MODEL_DELAY_MS=3000` first, which is
the ordering CI uses: the browser walk runs against the fast default, then the
model is slowed so a restart has something to land inside.

```text
COMPOSE_PROJECT_NAME=tiny-hermes-e2e DETERMINISTIC_MODEL_DELAY_MS=3000
docker compose -f deploy/compose/compose.yaml up -d --wait
uv run --no-sync python scripts/restart_drill.py

Restart drill against http://127.0.0.1:8000  (model delay 3000ms)

1. Worker restarted while executing
  picked up         seconds=0.12  sequence=2
  $ docker compose restart worker
  after restart     status=completed  seconds=32.08
  events            count=6  contiguous=True  leases=2  last=run_completed

2. Redis stopped, then restarted
  $ docker compose stop redis
  without redis     status=completed  pickup=0.56  seconds=6.05
  events            count=3  contiguous=True  leases=1  last=run_completed
  $ docker compose start redis
  with redis #1     status=completed  pickup=2.06  seconds=6.08
  with redis #2     status=completed  pickup=0.14  seconds=6.00
  with redis #3     status=completed  pickup=0.12  seconds=6.03

3. Worker killed holding a lease, Scheduler restarted
  picked up         seconds=2.08  sequence=2
  $ docker compose kill worker
  $ docker compose restart scheduler
  lease expired     status=queued  seconds=26.31
  $ docker compose start worker
  worker returned   status=completed  seconds=2.28
  events            count=6  contiguous=True  leases=2  last=run_completed

All three scenarios held. 125.7s
```

Run IDs and workspace IDs were printed and are omitted here only for length; the
drill prints no cookie, token, password, or Run input at all.

Three numbers in that transcript are worth reading rather than only passing:

- **32 seconds for the Worker restart.** A Worker asked to stop finishes the
  slice it is inside first (`worker_shutdown_grace_seconds = 20`). The Run is not
  waiting on recovery; it is waiting on politeness.
- **26 seconds for the lease to expire.** `worker_lease_seconds = 30` less the
  time already elapsed, plus a `scheduler_interval_seconds = 5` scan. That is the
  only mechanism that recovers a Worker killed with SIGKILL, and the drill proves
  it works across a process boundary rather than in-process.
- **2.06s, then 0.14s, then 0.12s after Redis returns.** The first Run after
  Redis comes back is picked up a whole poll interval late while the subscription
  is being re-established, then wake-ups resume. This is why the drill submits
  three and asks only that the channel delivers again — see §5.2.

### 3.5 Secret scanning

`git diff --check` reported nothing beyond a line-ending warning. A tracked-file
scan for secret-shaped values (AWS keys, GitHub tokens, `sk-` keys, Slack tokens,
PEM private keys, JWTs) returned no matches. The only tracked environment file is
`.env.example`. `scripts/restart_drill.py` contains a development-only default
password and bootstrap token, both overridable by environment variable and both
already present in `.env.example` and the Playwright setup.

## 4. What the slice proves

- **A person can drive the whole platform from a browser.** One signed-in session
  creates a Workspace, creates an Agent, writes a Draft, publishes v1, submits a
  Run, watches its timeline fill, and retries a failed Run — against the Compose
  stack, with no seeded row and no test-only endpoint.
- **The workspace is the address, not a setting.** Scope is a route parameter, so
  the console sends the `X-Workspace-Id` the URL implies. Asked for a Run under a
  Workspace this account has no standing in, it renders the server's refusal
  verbatim — "No such run exists in the selected workspace." — and shows no
  summary card. It performs no membership check of its own and substitutes no
  Workspace it does know about.
- **A resumed stream is exactly the whole history.** The acceptance walk reloads
  the page mid-Run, then asserts the timeline's sequence numbers are `1..n` with
  nothing skipped and nothing repeated, and that no gap marker is present for a
  Run minutes old.
- **The `EventSource` shape still works even though the console stopped using
  it.** `stream-contract.spec.ts` subscribes the way an `EventSource` would —
  session cookie, `workspace_id` in the query string, no request headers — and
  asserts `text/event-stream`, contiguous sequences, a `run_completed` frame, and
  the `id:`/`event:` framing an `EventSource` actually parses. Without a
  workspace anywhere the same route answers `400 workspace_required` rather than
  an empty subscription.
- **Restarting any one of the three processes loses no committed work.** All
  three drill scenarios end `completed` with a history numbered from one, nothing
  skipped and nothing repeated, and the drill is a CI step rather than a
  transcript — the check keeps holding as lease handling changes.
- **The panes the platform cannot fill are absent, not empty.** A test asserts by
  name that 父子任务树, 上下文和压缩事件, 产物, and Token 和费用 do not appear in
  the DOM. A panel reading "no data" would tell the user nothing happened, which
  is a different claim from "not built yet".
- **Nothing deletes state to prove state survives.** The drill refuses `down`,
  `rm`, `-v`, and `--volumes` at runtime rather than trusting the file to stay
  careful.

## 5. Deliberate deviations from the plan

1. **`useRunEvents` invalidates the Run snapshot twice when the first read has
   not landed.** TanStack Query v5 will not force-refetch a query that has never
   resolved: `query.fetch` only cancels an in-flight request when
   `dataUpdatedAt` is non-zero, and otherwise hands back the promise already in
   flight — a request sent before the event existed, which may answer with a
   state older than the event just rendered. The hook detects that case
   (`fetchStatus === "fetching" && dataUpdatedAt === 0`) and asks a second time
   once the first read has landed. This is a real library constraint, recorded in
   the code as well as here, not a retry-until-it-works.

2. **The Redis scenario submits three Runs and asserts the best of them, not
   one.** A wake-up is published once and never repeated, so a Worker that is
   mid-claim when one goes out simply misses it and waits a poll — the design
   says exactly that, and the first Run after Redis returns misses it almost
   every time. A single-sample assertion here would be a coin toss dressed up as
   a check. Asserting `min(pickups) < worker_idle_poll_seconds` fails only when
   the channel never delivers again, which is the thing that would actually be
   broken. The first version of the drill asserted the single sample and failed
   on a healthy platform; see §7.1.

3. **The Worker-restart scenario asserts progress past the interruption point,
   not a lease count.** The plan asked for a second `run_lease_acquired`, but
   `continue_once` runs both of its rounds inside one slice, so a second lease is
   evidence of the restart only if the Run had not already finished. The drill
   records `last_event_sequence` at the moment it restarts the Worker and
   requires the final sequence to exceed it, which says directly what the lease
   count says by implication — and fails with a message naming
   `DETERMINISTIC_MODEL_DELAY_MS` when the model is too fast for the restart to
   land inside a Run.

4. **`DETERMINISTIC_MODEL_DELAY_MS` was added to the Compose environment.** The
   setting existed and was wired into the Worker, but Compose did not pass it
   through, so a deployed stack was pinned to the 50 ms default and no restart
   could land inside a Run. It is now part of the shared `&app-env` anchor with
   the same default, and CI raises it for the drill step only.

5. **The drill runs after Playwright in CI, on a recreated stack.** Changing the
   model delay requires recreating the `api`, `worker`, and `scheduler`
   containers. `up -d --wait` does that and keeps the volumes, so the account the
   Playwright setup bootstrapped is still there and the drill signs in as it. The
   alternative — running the whole browser walk against a 3-second model — would
   only make the acceptance suite wait for nothing.

6. **`scripts` was added to pyright's `include`.** The drill is a real program
   that asserts things about the platform, so it is typechecked strictly like
   everything else.

7. **Playwright runs three projects, not one.** `setup` bootstraps and signs in
   once and saves the browser state; `foundation` deliberately starts signed out,
   because signing in through the form is what it is about; `console` reuses the
   saved state. The shared constants live in `tests/e2e/session.ts` rather than
   in `bootstrap.setup.ts`, because importing a file that registers a test would
   register that test a second time in whichever project imported it.

8. **The drill contains the words `down`, `-v`, and `rm` — in the guard that
   refuses them.** The plan said the script must not contain them at all. A
   literal reading would have left the rule as a comment, enforced by whoever
   edits the file next; `FORBIDDEN` makes it a runtime refusal that survives a
   careless edit. `compose("down")` raises before Docker is invoked. The
   intent — no command that removes state is ever issued — is met more strongly
   than by absence of the strings.

9. **The bootstrap setup tolerates `409 bootstrap_closed`.** CI always runs
   against a fresh stack and gets `201`. A local rerun reaches an already-opened
   platform, and `bootstrap_closed` is the only refusal that may mean — a wrong
   token still answers `403` and still fails the setup.

## 6. Known phase-2C limits

- **The Run list is not paginated.** The server returns everything it has and the
  console shows it. A `limit`/`cursor` contract the console pretends to honour
  while the server ignores it is a lie that gets found in production, so neither
  side pretends. Pagination belongs on the API first.
- **No parent/child task tree, no context or compaction events, no artifacts, and
  no token or cost accounting.** Design §20.3 describes all four; the platform
  produces none of the underlying data yet, so the console renders none of them
  in any form.
- **There is still no real model provider, no tools, no file handling, and no
  sandbox.** The deterministic provider remains the only one, and a Run's `input`
  is never sent anywhere.
- **No Playground.** The Draft editor writes a Draft and publishes it; there is
  no way to try a Draft before publishing.
- **The drill needs a slowed model.** It refuses to run below 1000 ms rather than
  quietly proving nothing, which means the CI step recreates the stack. That is
  honest but it is not free: the drill cannot be run against a stack tuned for
  interactive use without recreating it first.
- **The wake-up channel takes one poll interval to recover after Redis
  returns.** Correctness is unaffected — this is precisely the latency Redis was
  buying — but it is a real, measured behavior and it is documented rather than
  smoothed over.
- **CI has still never run.** `git remote -v` is empty; there is no remote and no
  Actions run. Every claim about the CI workflow rests on the same steps having
  been executed locally, in the same order, with the results recorded above. The
  workflow file is a hypothesis until a remote exists.

### Which scenarios are automated, and which are a recorded drill

| Scenario | Covered by |
|---|---|
| Draft → publish → submit → live timeline → retry | Playwright, in CI |
| Foreign-workspace Run refused by the server | Playwright, in CI |
| Resumed stream contiguous with no gap or repeat | Playwright, in CI |
| `EventSource`-shaped subscription and `400 workspace_required` | Playwright, in CI |
| §20.3 panes absent from the DOM | Playwright, in CI |
| `410` gap marker sized from `earliest_available_sequence` | Vitest with msw — a retention window cannot be forced in a browser walk |
| Draft conflict preserves input; control conflict refetches | Vitest with msw |
| Buttons rendered from `available_actions` alone | Vitest with msw |
| Worker, Redis, and Scheduler restarts | `scripts/restart_drill.py`, run above and wired into CI |

## 7. Redacted failure evidence

1. **The drill's first Redis assertion failed on a healthy platform.** It
   compared one pickup measurement against the idle poll interval and read
   `pickup=2.08s`, concluding the wake-up channel had not recovered. A six-Run
   probe showed the truth: `0.06, 0.09, 0.08, 0.11, 0.08, 2.12` — the channel was
   fine, wake-ups are fire-and-forget, and a Worker between waits misses one and
   pays exactly one poll. The assertion, not the platform, was wrong. Fixed as
   described in §5.2. Recorded because a check that fails on correct behavior is
   worse than no check: it teaches people to rerun until green.

2. **`foundation.spec.ts` failed once on a workspace count.** It read the list
   length before the list had loaded, then asserted two more than that. With one
   account now shared across every spec and dozens of Workspaces accumulated by
   drill runs, the race became visible. Fixed by counting after the first
   Workspace is on screen and asserting that the second creation adds exactly one
   row, which is what the test is actually about and cannot race.

3. **Playwright could not sign in at all, and the cause was not the code.** The
   local `tiny-hermes` stack had been bootstrapped during an earlier manual smoke
   check under a different account, so `POST /bootstrap` answered
   `409 bootstrap_closed` and the setup's credentials were never valid. Confirmed
   by reading `users` joined to `auth_identities` in the running database. Fixed
   without deleting anything: the same Compose file was brought up under
   `-p tiny-hermes-e2e`, giving a fresh platform on fresh volumes and leaving the
   original stack intact.

4. **The Agent-creation step hung on the modal.** The alias was derived from a
   scenario name, and `continue_once` contains an underscore, which
   `ALIAS_PATTERN` (`^[a-z0-9]+(?:-[a-z0-9]+)*$`) rejects — so the server refused
   and the dialog simply stayed open until the timeout. Fixed by hyphenating the
   alias and by asserting the dialog is hidden, so a refusal now fails at the
   point it happens instead of thirty seconds later somewhere else.

5. **`getByRole("option")` was never clickable.** rc-select renders a second,
   screen-reader-only option list carrying the same role, which is never visible;
   the role query found that one first and reported "element is not visible" for
   `id="scenario_list_0"`. Fixed with a helper that targets
   `.ant-select-item-option[title="…"]` inside the visible dropdown.

6. **A transient `403` on `POST /workspaces` in the drill.** The CSRF token had
   been percent-encoded into the cookie and was echoed back still encoded, so it
   did not match. Intermittent by nature — it depends on the random token
   containing a character that needs encoding. Fixed by unquoting the cookie
   before sending it as the header, which is what the Playwright specs already
   did.

7. **A build error the tests could not have caught.** `RunDetailPage` typed a
   `Descriptions` items array as `DescriptionsItemType[]`, which under
   `exactOptionalPropertyTypes: true` is not assignable to the optional `items`
   prop. Vitest passed; `pnpm web:build` failed at `RunDetailPage.tsx(307,10)`.
   Fixed with `type Rows = NonNullable<DescriptionsProps["items"]>`. Recorded
   because it is the case for keeping the build in the verification list rather
   than trusting the test run.

## 8. Phase-2C completion checklist

- [x] A duplicate alias returns `409 agent_alias_taken` against PostgreSQL.
- [x] Every scoped request carries `X-Workspace-Id` derived from the route
      parameter, and no page holds an ambient workspace.
- [x] `PUT` carries `X-CSRF-Token`, pinned by a test.
- [x] A browser session completes draft → publish → submit → live timeline →
      control against the Compose stack, with no test-only route and no seeded
      data.
- [x] A foreign Workspace's Run URL is refused by the server, not hidden by the
      console.
- [x] The stream resumes after a drop with no gap and no duplicate.
- [x] A `410` produces a visible gap marker sized from
      `earliest_available_sequence`, never a silently shortened timeline.
- [x] Run controls are rendered from `available_actions` alone.
- [x] A draft conflict preserves typed input and sends no automatic retry; a
      control conflict refetches and re-renders.
- [x] 取消, 重试, and 发布 send nothing before an explicit confirmation.
- [x] 父子任务树, 上下文和压缩事件, 产物, and Token 和费用 are absent from the
      DOM, asserted by name.
- [x] The `workspace_id` query-parameter stream form still has a test.
- [x] Worker, Redis, and Scheduler restarts lose no committed state, in a
      recorded run and as a CI step, with no volume deleted.
- [x] `docs/development.md` documents the console, the Run list's missing
      pagination, and how to run the drill.
- [x] All existing phase-1, 2A, and 2B checks pass unchanged (271 backend tests,
      62 web tests).
