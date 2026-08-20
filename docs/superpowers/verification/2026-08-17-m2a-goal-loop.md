# M2A goal loop and context budget — phase exit verification, 2026-08-17

## 1. Scope

This record covers **all of M2A**, both halves, specified in
`docs/superpowers/specs/2026-08-17-m2a-goal-loop-design.md`:

- **M2A-1** (`docs/superpowers/plans/2026-08-17-m2a-goal-loop.md`): the
  platform, not the model, decides that a Run is finished. Nine planned steps,
  all landed.
- **M2A-2** (`docs/superpowers/plans/2026-08-17-m2a-context-budget.md`, design
  §4.8–§4.9, product §7.4.2): a round is planned before it is sent. Nine
  planned steps, all landed.

One sentence for the pair: M2A-1 lets a Run go on for longer, and M2A-2 lets it
go on for longer *and still fit*.

Commits on `feat/goal-loop`, branched from `main` at `5ad0e00` (the M2
roadmap, PR #19):

| Commit | Result |
|---|---|
| `a988abb` | The platform decides a Run is finished, not the model. |
| `1a11b95` | The slice policy reads a verdict, not a stop reason. |
| `72dca66` | Check the claim before ending the Run, and say what failed. |
| `ea9c26e` | An administrator sets the round ceiling; a budget widens on purpose. |
| `a9fe91d` | `waiting_external` gets its first producer, and a deadline that wakes. |
| `3005511` | Say which round a Run is on, and why it is still going. |
| `5502829` | The M2A-1 exit criteria against evidence. |
| `e388974` | The M2A-2 plan. |
| `4810cab` | Decide what a round may send, before there is anything to send it to. |
| `01fa480` | An endpoint says how its window is counted, not just how big it is. |
| `1c20324` | A version states its segment budget; publishing checks the endpoint can serve it. |
| `65b7b42` | Plan the context before the round; pause rather than send what cannot fit. |
| `8ad0d41` | Say what the endpoint declared, and what the platform did to the context. |
| this record | The seven exit criteria against evidence. |

## 2. Environment

Windows 11, Python 3.12 via uv, Node via pnpm. Integration tests run against a
local PostgreSQL 17 container on port 54320. The end-to-end walk runs against
`deploy/compose/compose.yaml` brought up as an isolated project
(`-p tiny-hermes-e2e`) with its own volumes, torn down with `down -v`
afterwards; the pre-existing local demo stack was stopped for the duration and
restarted untouched.

Both halves were verified this way, the second time after M2A-2 landed: the
stack was rebuilt from this branch (`up -d --build --wait`) and the walk re-run
against it, so the numbers in §5 are one set covering both.

`SANDBOX_IMAGE_DIGEST` is **empty** on this host, so no sandbox container can
start here. That bounds what the walk can prove and is stated again in §6.

No token, password, or key material appears in this record.

## 3. M2A-1's five exit criteria

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

## 4. M2A-2's two exit criteria

The roadmap names two for this half. Both are about the same promise read from
two sides: the platform never sends a request it knows will be refused, and it
never makes room by making something unreachable.

### 4.1 A window that only fits the incompressible content is refused at publish, and overflows at runtime

**At publish.** `packages/backend/tests/unit/agents/test_context_budget_spec.py`.
An Agent whose segment targets sum to more than its endpoint's input allowance
is refused with `context_budget_unsatisfied`
(`test_targets_that_do_not_fit_are_refused_with_per_segment_advice`), and the
refusal carries a number for every segment rather than the word "too large" —
an author who wrote `40` and is told only "invalid" has no way to learn that
`20` was the answer. `RoundCeilingExceeded` set that rule in M2A-1; this
follows it.

The advice is advice: `test_the_advice_does_not_apply_itself` publishes nothing
and changes nothing, and `test_an_author_who_takes_the_advice_can_publish`
shows the same spec going through once the author has taken it. §7.4.2's
不会静默生效, in two tests.

An endpoint too small to hold even the floor is a different refusal,
`context_window_too_small`
(`test_a_window_that_cannot_hold_the_minimum_fails_at_publish`), because there
is no advice that would help.

**At runtime.** `tests/integration/runs/test_context_budget.py::test_a_round_that_cannot_be_made_to_fit_is_never_sent`.
A 32,000-character request against an endpoint whose whole input allowance is
9,472 tokens. The Run ends `paused(context_overflow)` with
`model.requests == []` and `consumed_model_calls == 0` — the provider is never
called, so nothing is spent on a request that could only have been refused —
and the transcript still holds all 32,000 characters.

Nothing is trimmed or compacted on that path, and no event says either was.
An event named for work that did not happen would be worse than none.

The unit twin, on the pure function:
`test_incompressible_content_that_does_not_fit_does_not_get_truncated` and
`test_a_conversation_that_cannot_be_compacted_small_enough_keeps_its_originals`.
The planner reports `fits=False` and hands the originals back; it does not
decide the Run's state, and it never truncates a request to make one fit.

### 4.2 A compaction can be followed back to the messages it covered

`tests/integration/runs/test_context_budget.py::test_an_old_conversation_is_compacted_with_its_range_and_ids`.
A persistent Session whose earlier Run left two 15,000-character turns, then a
new question. The `context_compacted` event carries `first_sequence`,
`last_sequence`, `covered`, and `message_ids` — one id per covered message,
asserted as `covered == len(message_ids)` — so the range is not a claim about
how many were covered but a list of which.

What the round was actually sent: the summary first, the question last and
whole. The originals are all still in `session_messages`; the compaction is a
record, never a deletion.

Trimming leaves the same kind of trail one step earlier
(`test_a_tool_result_too_large_to_carry_is_trimmed_and_recorded`): the
`context_trimmed` event names the segment and the `call_id`s it touched, the
tool message stays in place answering the same call with a stub that says how
large the output was, and the transcript still holds every one of the 40,000
characters. A hole where a result was would teach the model the command never
ran.

`test_a_conversation_inside_the_window_is_sent_as_it_stands` is the fourth and
most common case: nothing trimmed, nothing compacted, no event — because
nothing happened.

### 4.3 Two rules that hold across every test in this half

Neither is a criterion the roadmap names; both are why the code is arranged the
way it is, and both are asserted rather than asserted about.

**Every number is a plan estimate, never usage.** `test_the_planner_never_reports_a_number_as_usage`
checks that the result has no `tokens` and no `usage` attribute at all, so a
caller cannot read one as a count by accident. `UsageQuality` still has no
`estimated` member. No estimate reaches `consumed_tokens`, a checkpoint, or any
user-facing number without the word "estimate" beside it — the console
sentences say so in both locales.

**The window comes from the endpoint, computed rather than guessed.**
`test_a_shared_window_reserves_the_output_and_a_separate_one_does_not`: two
endpoints declaring the same 8,000 have input allowances of 6,000 and 8,000
depending on `context_accounting`. The Worker reads it off the endpoint row
each round rather than freezing it into the Agent Version, because the window
belongs to the endpoint an administrator maintains.

## 5. Everything green

Re-run in full after M2A-2 landed, at `8ad0d41`.

| Suite | Command | Result |
|---|---|---|
| Backend unit | `uv run pytest packages/backend/tests/unit -q` | 786 passed |
| Backend integration | `uv run pytest packages/backend/tests/integration -q` | 417 passed, 17 skipped, **1 failed** (see below) |
| Lint | `uv run ruff check packages/backend` | All checks passed |
| Types | `uv run pyright` | 0 errors, 0 warnings |
| Frontend | `vitest run --no-file-parallelism` | 102 passed (16 files) |
| Types (web) | `tsc --noEmit -p tsconfig.json` | clean |
| Lint (web) | `eslint . --max-warnings=0` | clean |
| End to end | `playwright test --config tests/e2e/playwright.config.ts` | 7 passed, **1 failed** (see §6) |

Two failures, both environmental, both reproduced on a clean checkout of this
branch's parent:

- `tests/integration/sandbox/test_engine_workspace.py::test_execute_feeds_stdin_and_the_helper_writes_atomically`
  fails on Windows with `TypeError: 'NpipeSocket' object does not support the
  context manager protocol`. The Docker SDK's named-pipe socket, not this
  platform's code. CI runs on Linux.
- The frontend suite fails two to five tests when vitest runs files in
  parallel on this host — always `findBy*` timeouts, always in files this slice
  does not touch (`App`, `ApiKeysPage`, `ModelEndpointsPage`, `SecretsPage`),
  and a different set each run. `--no-file-parallelism` passes all 102. The same
  flakiness reproduces with this branch's changes stashed, so it is machine
  contention rather than a regression.

The one end-to-end failure was checked rather than assumed to be the same one:
its Run's `run_failed` payload reads `"failure_reason": "sandbox_not_configured"`,
and the database the walk ran against holds no `context_trimmed` or
`context_compacted` event at all — the walk never reached this half's code, for
the reason given in §6.

## 6. What the end-to-end walk could not prove here

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

**M2A-2 is not reachable through the walk at all, and that is a fact about the
walk.** Every Agent it publishes uses a `deterministic` model policy, which
names no endpoint, so `ExecutionContext.window` is `None` and the planner is a
no-op on every Run the walk creates. The half is covered by the unit and
integration suites instead, and by the console tests for what a reader sees.
What the walk *does* prove for this half is the migration: the stack came up
from empty volumes, so `20260817_0013_context_budget` applies to a database
that has never seen it — the only suite here that proves that, since the
integration tests truncate a schema rather than build one.

## 7. Things worth carrying forward

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

**Oldest-first compaction walks straight at the message that may never go.**
A fresh Run states its request first and everything after it is the work, so
the compaction boundary — which was bounded only by 最近历史 — could swallow
the current user request on exactly the conversation that most needed room.
§7.4.2 says that message is kept whole; the planner would have made a
conversation fit by summarizing away the question it was answering. Found while
wiring the Worker, before any Run met it. The boundary is now bounded by the
request as well, and
`test_a_compaction_never_reaches_the_request_it_is_making_room_for` asserts
that this conversation overflows with its originals intact rather than fitting
wrongly. Overflow is the correct answer there, which is the point: the planner
is not allowed to buy a fit with something it may not spend.

**A new check refuses fixtures too.** The publish-time budget check landed and
`test_a_run_reaches_completed_with_text_the_endpoint_produced` began returning
422: its stand-in endpoint declared 8,192 with 512 reserved, an allowance of
7,680 against a default segment sum of 9,472. The check was right and the
fixture was not — a test endpoint no Agent could be published against had been
describing a deployment that cannot exist. Widened in `1c20324`.

## 8. Not claimed

- Any wait kind other than `timer`. Approval is M2C, child runs are M2E.
- Any verified tokenizer. The registry ships empty on purpose (§4.8): every
  endpoint gets the conservative character bound, and a declared `tokenizer`
  name is recorded without being trusted. The console says so where the field
  is edited.
- Skill summaries and memory as trimming steps two and three. Both segments are
  allocated and always empty in this phase (design §7); the order has their
  place in it, and M2B and M2D fill them without moving it.
- Any benchmark number. This host is not the §24.1 reference shape and no
  benchmark was run.
