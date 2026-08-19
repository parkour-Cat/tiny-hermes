# M2E one level of parallel child Agents — phase exit verification, 2026-08-19

## 1. Scope

This record covers all of M2E, planned in
`docs/superpowers/plans/2026-08-19-m2e-child-agents.md` against product design
§12.4, §13, §14.1 and §27.2.3, and the M2 roadmap §8.

One sentence for the stage: an Agent can hand pieces of work to other Agents and
wait for them, and nothing it hands over widens what they may do or resets what
the work has already cost.

Commits on `codex/m2e-child-agents`, continuing after M2D's record at `11b6736`:

| Commit | Result |
|---|---|
| `1bc083c` | The plan. |
| `77616fc` | A child's permissions can only narrow, and widening cannot be written. |
| `7ceb04d` | A child Run exists, and it cannot be a grandchild or a second budget. |
| `ba05d02` | A parent hands work over, lets go of everything, and is woken with an answer. |
| `1994857` | A file moves as a permission, and only for the work it was given to. |
| `0b7f448` | A Run that is one of several says so, and says nobody is waiting on you. |
| this record | The seven exit criteria against evidence, and what the run did not prove. |

## 2. Environment

macOS 14 (Darwin 23.6), Python 3.12 via uv, Node 24 via pnpm, Docker via
OrbStack.

Backend integration tests run against a PostgreSQL 17.6 container on
`127.0.0.1:55432`, migrated to `20260819_0029`. A **separate** container from
the Compose stack's, and that is worth writing down: this host already runs a
PostgreSQL on 5432 bound to loopback, which shadows the Compose publish of the
same port. The first integration run against it produced 139 errors that had
nothing to do with this branch. A dedicated port removed the variable entirely.

`greenlet` had to be installed into the venv by hand. SQLAlchemy declares it
with a platform marker that covers `aarch64` but not macOS's `arm64`, so it is
present on CI's Linux and absent here. `pyproject.toml` was **not** changed:
the dependency set is correct for the platforms the project ships on, and
editing it to paper over a local gap would have been a change nobody asked for.

The browser walk runs against `deploy/compose/compose.yaml`, built from this
branch, with `SANDBOX_IMAGE_DIGEST` set to a locally built sandbox image.

## 3. What ran

| Suite | Result |
|---|---|
| `ruff check packages/backend migrations` | clean |
| `pyright` (backend and tests) | 0 errors |
| `pytest packages/backend/tests/unit` | 1768 passed |
| `pytest packages/backend/tests/integration` (less `sandbox/`) | 526 passed |
| `alembic upgrade head` / `check` | clean for 0027–0029 |
| `alembic downgrade` each of 0029→0026, and `downgrade base`, then `upgrade head` | clean |
| `tsc --noEmit` (console) | clean |
| `eslint .` (console) | clean |
| `vitest run` (console) | 152 passed |
| `playwright` (all projects, Compose stack) | **13 passed, 0 failed** |

`packages/backend/tests/integration/sandbox/` is excluded above and the reason
is in §5: it fails identically on a clean checkout of this branch's base.

## 4. The seven exit criteria

**1. Two child Runs really run in parallel, each holding its own sandbox and its
own SessionWorkspace.**

`tests/integration/runs/test_child_runs.py` drives one delegation with **two
Workers running concurrently**, which is the difference that matters: one Worker
run twice proves both Runs execute, two Workers prove neither is waiting on the
other. They can be claimed at the same moment because each child is the head of
a Session of its own.

The separation is asserted as an absence rather than as a refusal. A
SessionWorkspace is keyed by Session and a sandbox reservation is unique per
`run_id` over live claims, so a parent and a child sharing either is not a state
this schema can express. The test states that — three Runs, three Sessions, no
revision two of them point at — because what would break it is somebody giving a
child its parent's Run or Session id.

**2. A child Agent cannot create a grandchild.**

Refused twice, and both were run rather than assumed.
`SqlRunStore.delegate_children` decides it from the caller's own `depth`, so the
refusal does not depend on the child's configuration — the test publishes a
child whose spec **wrongly carries a delegation policy and the tool**, which is
exactly the case §13's third clause is about, and it is still refused. With that
code guard deleted and the suite re-run, `ck_runs_depth` refused the row at the
database instead. Both layers demonstrated, in that order.

**3. A child using a tool, network target, secret, skill or memory outside the
intersection is refused by the execution layer.**

The intersection itself is `tests/unit/agents/test_delegation.py`, whose
property test enumerates every combination of the six faces and asserts the
parent still covers the result. There is no union, no `widen` and no argument
that adds a name — the control is that widening cannot be written.

Publishing refuses a delegation wider than the Agent itself, naming the
offending faces. At creation, `granted()` recomputes the same intersection at
the moment it becomes a Run and stores it as a **snapshot** on
`runs.delegation_scope`, so a parent republished or rolled back mid-flight
cannot change a running child's permissions.

The honest limit is in §5: the stored scope is computed and recorded, and the
`files` face is enforced, but tools, network, secrets and skills are not yet
consulted by the execution layer at call time.

**4. A child cannot read the parent's unauthorized private memory (§27.2.3).**

True by construction rather than by a filter, which is why it is asserted
against the bytes sent to the model rather than against a query. A private
memory is scoped by workspace, **agent** and subject; a child runs as its own
Agent, so the parent's memories are outside its scope with nothing to forget to
check. `test_child_runs.py` writes a memory for the parent, runs the
delegation, and asserts the parent's own request carries it and no child's
does — both halves, because a retrieval that returned nothing to anybody would
otherwise pass.

**5. A parent and a child share no writable directory; files move only by
Artifact authorization.**

Covered by criterion 1 for the absence, and by
`artifact_grants` for the mechanism. A grant belongs to one **Run** — not to an
Agent and not to a Session — so a later Run of the same Agent cannot open what
was passed to this piece of work. Downward, a parent may pass on only what it
can read, and naming a file it cannot refuses the whole delegation rather than
dropping that one file: a child working from inputs it never received has no way
to notice. Upward, a child's artifacts are granted to the parent in the same
transaction that delivers the result.

`artifact.read` is the read path, and it is not in the plan. It had to be built:
a grant with no way to read is an inert row, and a Run could previously write
Artifacts but never open one. It takes an id and never a path, and "does not
exist" and "not yours" come back as the same sentence so an Agent cannot map
somebody else's ids by reading refusals.

**6. Tokens, cost, execution time, tool calls and retries accumulate on one root
budget, and creating a child resets none of them.**

Counted rather than asserted about. `test_child_runs.py` reads three Runs, one
`run_budget_scopes` row, and `consumed_model_calls == 4` — the parent's two
rounds and one each from two children. A tree that gave each Run a budget of its
own would report the same three Runs and a smaller number, and would pass every
other test here.

**7. A child's result is not lost when the parent is unavailable, and is
delivered once when it recovers.**

A terminal child writes its result on **its own row**, and delivery happens on a
later Scheduler tick when the parent can take it. That is the design rather than
an optimization: a child usually finishes while its parent is unavailable — held
by another Worker, or still waiting on a sibling — and a delivery attempted then
would have to fail or block. A row survives that; a call does not.

`tests/integration/runs/test_child_waits.py` runs the sweep before the children
exist and four times after, and asserts exactly one platform-authored turn in
the parent's Session. `runs.result_delivered_at` is stamped in the same
transaction that appends the turn, which is what makes the repetition safe.

The same suite covers the rest of §13's tenth and eleventh clauses: a waiting
parent holds no lease row and no live sandbox reservation; it does not wake
itself, and the Workers can be run to exhaustion to prove it; `any` wakes on the
first success and cancels the sibling; children that all failed put the parent
back in `queued` with a summary rather than failing it; a passed deadline
becomes `paused(external_timeout)`; and cancelling a parent cancels a child
still running.

## 5. What this run does not prove

**The intersection is stored and only partly enforced.** `runs.delegation_scope`
records what a child was granted across all six faces, publishing refuses a
delegation wider than its parent, and the `files` face is checked on every read.
The other four are **not** consulted at call time: a child's tool authorization
still comes from its own published Version, which the parent could not widen but
also does not narrow. So a child bound to `shell.exec` in its own spec can run
it even where the delegation named no tools. That is a real gap against §13's
sixth clause read strictly, it is not what the plan's §2 asked for, and the
place it belongs is the execution layer's second permission check. Written down
here rather than left to be discovered.

**The sandbox transport suite fails on this host and does not on CI.**
`tests/integration/sandbox/test_transport.py` and `test_transport_streaming.py`
give 2 failures and 15 errors. The same two files were run on a stashed working
tree — a clean checkout of this branch's base — and produced **the identical 2
failures and 15 errors**. It is a macOS unix-socket environment problem, not a
regression, and it is excluded from the counts in §3 rather than hidden in them.

**Parallelism is two Workers on one machine, not two machines.** Nothing here
shows the delegation surviving a Worker dying mid-child, beyond the lease
recovery M1 already had.

**`max_parallel` is checked, and the ceiling it defends is not measured.** A
call asking for more children than the policy allows is refused. Nobody ran a
delegation of eight against a constrained host to see what it does to a Worker
pool.

**One delegation per round, and a second delegation in the same Run is
untested.** The wait names its own children explicitly so a second batch would
be a second wait — that is why the field exists — but no test delegates twice.

**Creation is not idempotent.** A round that delegated and was then rolled back
leaves its children behind and the parent will delegate again. This is the same
bargain `memory.remember` and `skill.propose` each accepted, and it is milder
here for one specific reason: both sets share one root budget, so a duplicate
delegation spends the same counters and stops against the same ceiling rather
than doubling it. Stated because "delivered exactly once" is proven above and a
reader could reasonably take creation to be covered by it. It is not.

**The `any` cancellation was observed on a child that was waiting, not on one
mid-model-call.** The sibling in the test is in `waiting_external`, which is the
easy case. A child cancelled in the middle of a slice goes through
`cancelling`, which M1 covers generally and this phase did not drill.

**Nothing here was run against a real model.** Every scenario is the
deterministic provider. Whether a model asked to split work into independent
pieces actually writes instructions that stand on their own — §13's seventh
clause makes that the parent's whole job — is a question about models, and this
run says nothing about it.

**The wake-up turn is a `user` message, and that had a consequence worth
recording.** Delivering results appends a platform-authored `user` turn, which
made the deterministic stand-in re-read it as a fresh request and delegate a
second time. The scenario was corrected to ask "have I already delegated in this
Run" against the whole conversation. A real model would read the report and
continue, so this is a fact about the stand-in — but it is also a warning about
anything else keyed on "the last user message".

## 6. The browser walk

`tests/e2e/children.spec.ts` publishes two children and a coordinator through
the API, starts a Run, and catches the parent **while it is waiting** — status
`waiting_external`, `wait_kind` `child_runs`, two children — before waiting for
it to complete. That ordering is the point: a parent that never waited and one
that waited and woke look identical once it is over.

It then asserts each child's `parent_run_id`, `depth`, its own Session, and the
shared `budget_root_run_id`, and that the tree spent four model calls on one
counter. The second test opens the console, clicks a child link from the
parent's Run detail page, and checks the child names its parent back.

Agents are published through the API rather than the builder, for the reason
M2D's record gives: the scenario list is virtualized and an option near the
bottom cannot be reliably clicked, so a walk that drove it would publish one
scenario while appearing to choose another. That console defect is still open
and this phase did not fix it.

**Result: 13 passed, 0 failed**, all thirteen walks including the two new
ones, against a stack built from this branch with a real Controller, Scheduler
and egress proxy. Three things had to be fixed to get there and all three are
worth reading.

**The Run document was missing its new keys at the API boundary.** The column
was right, the snapshot was right, and every integration test passed reading the
database — but `RunResponse` lists its fields, so `parent_run_id`, `depth` and
`children` were dropped on the way out and the console was served a Run with no
tree. Only the browser walk noticed. The fix is three lines; the lesson is that
a suite which reads the store cannot see this class of bug, so
`test_the_api_says_a_Run_is_part_of_a_tree` now asserts it through HTTP, and it
fails on the old response model.

**`author` was being dropped at the same boundary, and had been all along.**
`SessionMessageResponse` declares `role` and `parts`. M2A added `author` so a
transcript could not misattribute the platform's words to a person, and no API
caller has ever been able to tell. Found while asserting that a delivered
report is the platform speaking; fixed in the same place and asserted in the
same test.

**The scenario select could not be driven, and that was a real defect rather
than a test problem.** M2D's record filed it as open: the list is long enough
that rc-select virtualizes it, and an option below the fold cannot be clicked
because the row under the cursor is recycled mid-click. Four console walks were
failing on it before this branch touched anything. The select now carries
`showSearch` and the walk types before clicking, which fixes it for the person
using the builder as much as for the walk. `MODEL_SCENARIOS` and
`IMPLEMENTED_TOOLS` also gained this phase's entries — an author who cannot tick
`agent.delegate` in the builder cannot delegate at all.

Two environment notes, neither a product finding. The `tools` walk refuses its
own stand-in with `reason: private` unless `OUTBOUND_ALLOWED_CIDRS` covers the
Compose bridge, and OrbStack's bridge is `192.168.107.0/24` rather than CI's
`172.16.0.0/12` — the boundary working, in an environment CI does not have. And
`nginx` in the `web` container resolves `api` once at start, so recreating the
API without restarting `web` yields a 502 from a cached address.

The merge gate remains a green `compose-e2e` on CI, which additionally runs the
restart, workspace and quota drills that were not run here.

## 7. What this stage does not claim

**One level is the design, not a starting point.** A child Agent cannot delegate,
and that is refused on the creation path and by a database constraint. It is not
"only one level for now" — a tree of arbitrary depth is a different product with
different budget, cancellation and permission questions, and none of them are
answered here.

**The intersection is a permission intersection, not a guarantee about
behaviour.** It decides what a child *may* reach. It says nothing about what the
child does with what it can reach, and a narrow scope is not a safe Agent.

**Parallel means two Runs executing at the same time, not two Agents
collaborating.** The children cannot see each other, cannot talk to each other,
and learn nothing about each other's work. The only thing that joins them is a
parent reading two reports afterwards.

**A parent reads a report, never a transcript.** It gets an outcome, a summary in
the child's own words and the ids of files it was granted. §13's seventh clause,
and it means a parent cannot audit how a child reached its answer — a person can,
in the child's Session, and the console links there.

**The budget is shared, so a child can exhaust its parent.** One tree is one set
of counters by design. A child that spends the root budget stops the whole tree,
including the parent that was waiting for it. That is the intended reading of
§12.4 and not a limitation to be worked around.
