# M2A goal loop — phase exit verification, 2026-08-17

## 1. Scope

This record covers M2A-1 of the M2 roadmap, specified in
`docs/superpowers/specs/2026-08-17-m2a-goal-loop-design.md` and planned in
`docs/superpowers/plans/2026-08-17-m2a-goal-loop.md`: the platform, not the
model, decides that a Run is finished. Nine planned steps, all landed.

M2A-2 (context budget, trimming, compaction) is **not** in this slice and is
not claimed here.

Commits on `feat/goal-loop`, branched from `main` at `ce9bf35`:

| Commit | Result |
|---|---|
| `a988abb` | The platform decides a Run is finished, not the model. |
| `1a11b95` | The slice policy reads a verdict, not a stop reason. |
| `72dca66` | Check the claim before ending the Run, and say what failed. |
| `ea9c26e` | An administrator sets the round ceiling; a budget widens on purpose. |
| `a9fe91d` | `waiting_external` gets its first producer, and a deadline that wakes. |
| `3005511` | Say which round a Run is on, and why it is still going. |
| this record | The five exit criteria against evidence. |

## 2. Environment

Windows 11, Python 3.12 via uv, Node via pnpm. Integration tests run against a
local PostgreSQL 17 container on port 54320. The end-to-end walk runs against
`deploy/compose/compose.yaml` brought up as an isolated project
(`-p tiny-hermes-e2e`) with its own volumes, torn down with `down -v`
afterwards; the pre-existing local demo stack was stopped for the duration and
restarted untouched.

`SANDBOX_IMAGE_DIGEST` is **empty** on this host, so no sandbox container can
start here. That bounds what the walk can prove and is stated again in §4.

No token, password, or key material appears in this record.

## 3. The five exit criteria

### 3.1 A task needing three-plus rounds of tool calls is ended by the judge

`tests/integration/runs/test_worker_goal.py::test_a_task_that_takes_several_rounds_of_work_is_ended_by_the_judge`.

Three rounds that each call `shell.exec` and do a piece of the work, then a
fourth that claims completion. The claim is checked with the Agent's declared
`verification_command` before it is believed. The Run ends `completed` with
`goal == {"round": 4, "outcome": "done", "unmet": []}`, and the timeline
carries four verdicts: `continue, continue, continue, done`.

What ends the Run is the judge accepting a *verified* `done` — not the model
announcing one, and not a budget running out. The test drives the Worker until
the Run settles rather than exactly once, so the round count is asserted to
survive a slice boundary wherever the platform chooses to put one.

### 3.2 A `done` the verification contradicts is not accepted

`test_a_claim_the_verification_contradicts_does_not_end_the_run`: the model
reports completion every round and the check fails every round, so the Run
never completes — it works until the shared budget stops it.

Supporting: `test_a_verification_that_passes_lets_the_claim_stand`,
`test_a_run_that_was_wrong_once_may_still_finish`,
`test_a_missing_artifact_is_a_check_that_did_not_pass`,
`test_an_artifact_that_exists_is_a_check_that_passed`,
`test_an_agent_that_declares_nothing_is_never_verified`.

A check the platform could not run is neither an acceptance nor a rejection:
`test_a_verification_that_cannot_run_pauses_for_a_person` and
`test_a_verification_that_ran_out_of_time_is_not_a_verdict_either` both land
in `paused(operator)`.

### 3.3 The round ceiling is resumable, and counters do not reset

`tests/integration/runs/test_budget_expansion.py`. A Run that hits the ceiling
enters `paused(limit)` and offers no resume until the budget is widened
(`test_a_paused_run_offers_no_resume_until_the_budget_is_widened`); widening
reopens it **without** resetting a counter
(`test_widening_the_budget_reopens_the_run_without_resetting_a_counter`); it
then goes back to work and spends only the room it was given
(`test_the_run_goes_back_to_work_and_spends_the_room_it_was_given`).

The expansion command carries no `consumed_*` at all, so an expansion that
quietly zeroed a counter is not a thing this API can express.

### 3.4 `wait` releases the lease and the sandbox, is woken, and can time out

`tests/integration/runs/test_external_wait.py`: a round that asks to wait
enters `waiting_external` with `wait_kind="timer"` and a deadline equal to the
duration asked for; the waiting Run holds no lease; an Agent that binds only
`platform.wait` never opens a sandbox at all; the Scheduler puts it back in the
queue at the deadline and the woken Run finishes the work it stopped in the
middle of.

The overrun half is `tests/integration/runs/test_scheduler.py`: a
`waiting_external` Run whose deadline passed with `wait_kind = 'child_runs'`
becomes `paused(external_timeout)`.

**Stated plainly:** the two halves cannot both be reached through one wait kind
in M2A, and that is by design rather than a gap in coverage. A timer's deadline
is the platform's own, so reaching it *is* the wake; only a kind whose wake
comes from outside can be said to have nobody answer. The only such kinds are
approval (M2C) and child runs (M2E), so `paused(external_timeout)` is exercised
here at the Scheduler with a row in that state, not end to end.

### 3.5 Everything green

| Suite | Command | Result |
|---|---|---|
| Backend | `uv run pytest packages/backend/tests -q` | 1165 passed, 17 skipped, **1 failed** (see below) |
| Lint | `uv run ruff check packages/backend` | All checks passed |
| Types | `uv run pyright` | 0 errors, 0 warnings |
| Frontend | `npx vitest run --no-file-parallelism` | 94 passed (16 files) |
| Types (web) | `npx tsc --noEmit` | clean |
| Lint (web) | `npx eslint . --max-warnings 0` | clean |
| End to end | `npx playwright test --config tests/e2e/playwright.config.ts` | 7 passed, **1 failed** (see §4) |

Two failures, both environmental, both reproduced on a clean checkout of this
branch's parent:

- `tests/integration/sandbox/test_engine_workspace.py::test_execute_feeds_stdin_and_the_helper_writes_atomically`
  fails on Windows with `TypeError: 'NpipeSocket' object does not support the
  context manager protocol`. The Docker SDK's named-pipe socket, not this
  platform's code. CI runs on Linux.
- The frontend suite fails two to five tests when vitest runs files in
  parallel on this host — always `findBy*` timeouts, always in files this slice
  does not touch (`App`, `ApiKeysPage`, `ModelEndpointsPage`, `SecretsPage`),
  and a different set each run. `--no-file-parallelism` passes all 94. The same
  flakiness reproduces with this branch's changes stashed, so it is machine
  contention rather than a regression.

## 4. What the end-to-end walk could not prove here

`SANDBOX_IMAGE_DIGEST` is empty on this host, so
`console.spec.ts > the builder binds a tool, playground sends, and rollback
restores v1` fails: its Run ends `failed` with `sandbox_not_configured`. That
test predates this slice and fails for the same reason on `main` here. CI sets
the digest.

The new walk —
`console.spec.ts > a run that has not finished says which round it is on and
why` — **passed on this host**, and passed *because* an Agent that binds only
`platform.wait` opens no sandbox. The one console scenario this slice adds is
therefore the one that did not need the missing image, which is a consequence
of step 7's design rather than a convenience.

## 5. Two things worth carrying forward

**A round that changed no state wrote no events at all.** `record_slice`
returned early when `command.signal is None`, and took that round's events with
it. So the Run whose timeline most needed explaining — one that worked for
several rounds without a transition — was exactly the Run whose timeline was
empty. Found by an assertion that only round 2's verdict had landed. Fixed in
`3005511`; `test_every_round_leaves_its_verdict_on_the_timeline` is the guard.

**The console can make a platform state unreachable without breaking
anything.** `platform.wait` shipped in `a9fe91d` with tests, but
`IMPLEMENTED_TOOLS` in the web app is a hand-kept list, so no one could bind it
from the builder and `waiting_external` could not be reached through the UI at
all. Three deterministic scenarios were missing the same way. Nothing failed;
the capability was simply invisible. Filled in `3005511`.

## 6. Not claimed

- M2A-2: context budget, trimming, compaction (design §4.8–§4.9).
- Any wait kind other than `timer`. Approval is M2C, child runs are M2E.
- Any benchmark number. This host is not the §24.1 reference shape and no
  benchmark was run.
