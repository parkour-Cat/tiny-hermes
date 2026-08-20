# Sandbox Cleanup Race Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent a stale Scheduler cleanup failure from changing an already released sandbox reservation back to `isolated`, then rebuild and verify the M2A–M2D branch stack.

**Architecture:** Keep the Scheduler's fail-closed cleanup flow unchanged and enforce the state boundary inside `SqlSandboxStore.isolate()`. The store locks and refreshes the reservation row, isolates only statuses that still hold a live claim, and returns an already released reservation without writing it.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, PostgreSQL 17, pytest/pytest-asyncio, Ruff, Pyright, GitHub Actions.

---

## Scope and file map

- Modify `packages/backend/tests/integration/runs/test_scheduler_sandboxes.py`: add a deterministic Controller stand-in and regression test that reproduces the stale cleanup ordering without timing or sleeps.
- Modify `packages/backend/src/tiny_hermes/sandbox/infrastructure/sql_store.py`: lock and refresh the reservation row inside `isolate()`, then preserve `released` as terminal.
- Do not modify `packages/backend/src/tiny_hermes/runs/application/scheduler.py`: its cleanup exception path remains fail-closed and keeps calling `isolate()`.
- Use a temporary stack-rebuild worktree to rebase M2B, M2C, and M2D after M2A is fixed. Do not modify or push `codex/m2e-child-agents`.
- Do not create a pull request, operate on PR #8, delete `feat/goal-loop`, or touch `.claude/` and `.pnpm-store/`.

Local integration commands assume the repository's test PostgreSQL database is available at `localhost:5432`, as documented in `docs/development.md`. The unrelated Windows-worktree benchmark failure remains outside this change.

### Task 1: Add a deterministic regression for the stale cleanup ordering

**Files:**

- Modify: `packages/backend/tests/integration/runs/test_scheduler_sandboxes.py`
- Test: `packages/backend/tests/integration/runs/test_scheduler_sandboxes.py`

- [ ] **Step 1: Add a Controller stand-in that releases the selected reservation before reporting cleanup failure**

Place this class after `StandInController`:

```python
@dataclass
class ReleaseThenFailCleanup:
    """A concurrent cleanup wins after the Scheduler selected stale work."""

    sessions: async_sessionmaker[AsyncSession]

    async def cleanup(self, *, run_id: UUID, sandbox_id: UUID) -> None:
        del sandbox_id
        async with self.sessions.begin() as session:
            store = SqlSandboxStore(session)
            reservation = await store.live_for_run(run_id)
            assert reservation is not None
            await store.release(reservation.id)
        raise RuntimeError("the stale Scheduler cleanup was refused")
```

The separate transaction commits `released` before the Scheduler enters its existing exception handler. This makes the race ordering deterministic and does not rely on sleeps.

- [ ] **Step 2: Add the failing regression beside the existing cleanup-failure test**

```python
async def test_a_stale_cleanup_failure_cannot_reisolate_a_released_reservation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    run_id, _ = await a_kept_reservation(
        sessions, expires_in=-timedelta(seconds=1)
    )

    await (await scheduler(sessions, ReleaseThenFailCleanup(sessions))).run_once()

    assert await reservation_status(sessions, run_id) == ReservationStatus.RELEASED.value
```

- [ ] **Step 3: Run only the regression and verify RED**

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test"
$env:TEST_DATABASE_URL = $env:DATABASE_URL
uv run --no-sync alembic upgrade head
uv run --no-sync pytest packages/backend/tests/integration/runs/test_scheduler_sandboxes.py::test_a_stale_cleanup_failure_cannot_reisolate_a_released_reservation -v
```

Expected: the test fails because the final status is `isolated` instead of `released`. A connection or migration failure is not the expected RED and must be fixed before proceeding.

- [ ] **Step 4: Commit the regression evidence**

```powershell
git add packages/backend/tests/integration/runs/test_scheduler_sandboxes.py
git commit -m "test(sandbox): reproduce stale cleanup reservation race"
```

### Task 2: Make `released` terminal inside `SqlSandboxStore.isolate()`

**Files:**

- Modify: `packages/backend/src/tiny_hermes/sandbox/infrastructure/sql_store.py`
- Test: `packages/backend/tests/integration/runs/test_scheduler_sandboxes.py`

- [ ] **Step 1: Replace `isolate()` with a locked, refreshed state transition**

Use the existing `select`, `LIVE_RESERVATIONS`, and `ReservationStatus` imports. Replace only the method body shown below; do not add Scheduler error-string handling or change Controller lease rules.

```python
    async def isolate(self, reservation_id: UUID, *, reason: str) -> SandboxReservation:
        found = await self._session.execute(
            select(SandboxReservationRow)
            .where(SandboxReservationRow.id == reservation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        row = found.scalar_one_or_none()
        if row is None:
            raise UnknownReservation
        if ReservationStatus(row.status) not in LIVE_RESERVATIONS:
            return _reservation(row)
        row.status = ReservationStatus.ISOLATED.value
        row.isolation_reason = reason
        # A deadline on an isolated claim would put it in the Scheduler's expiry
        # scan, which destroys; an isolated container needs confirming, not
        # destroying on a timer.
        row.idle_expires_at = None
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _reservation(row)
```

`with_for_update()` serializes this decision with concurrent writers. `populate_existing=True` ensures a row already present in the SQLAlchemy identity map is refreshed rather than reused with stale attributes. `RELEASED` is the only non-live reservation status, so it returns unchanged; active, kept, and already isolated reservations retain the existing fail-closed transition.

- [ ] **Step 2: Run the regression and verify GREEN**

```powershell
uv run --no-sync pytest packages/backend/tests/integration/runs/test_scheduler_sandboxes.py::test_a_stale_cleanup_failure_cannot_reisolate_a_released_reservation -v
```

Expected: `1 passed`.

- [ ] **Step 3: Run the whole Scheduler sandbox module**

```powershell
uv run --no-sync pytest packages/backend/tests/integration/runs/test_scheduler_sandboxes.py -v
```

Expected: all tests pass, including `test_a_cleanup_that_cannot_be_confirmed_leaves_it_isolated` and `test_an_isolated_reservation_is_retried_rather_than_forgotten`. These existing tests prove that a genuinely live but unconfirmed reservation is still isolated and retried.

- [ ] **Step 4: Commit the minimal store fix**

```powershell
git add packages/backend/src/tiny_hermes/sandbox/infrastructure/sql_store.py
git commit -m "fix(sandbox): keep released reservations terminal"
```

### Task 3: Verify the corrected M2A branch locally

**Files:**

- Verify: `packages/backend/src/tiny_hermes/sandbox/infrastructure/sql_store.py`
- Verify: `packages/backend/tests/integration/runs/test_scheduler_sandboxes.py`

- [ ] **Step 1: Run formatting, lint, and type checks**

```powershell
$env:UV_CACHE_DIR = ".uv-cache"
uv run --no-sync ruff format --check packages/backend/src/tiny_hermes/sandbox/infrastructure/sql_store.py packages/backend/tests/integration/runs/test_scheduler_sandboxes.py
uv run --no-sync ruff check packages/backend
uv run --no-sync pyright
```

Expected: all three commands exit 0.

- [ ] **Step 2: Run backend unit tests and distinguish the known baseline failure**

```powershell
uv run --no-sync pytest packages/backend/tests/unit -v
```

Expected on this Windows worktree: 786 tests pass and only `packages/backend/tests/unit/scripts/test_benchmark_m1.py::test_shape_only_exits_nonzero_when_the_host_is_too_small` fails because it treats the worktree `.git` file as a directory. Any additional failure belongs to this change and must be resolved. The Linux CI unit job must pass completely.

- [ ] **Step 3: Run the complete integration suite against PostgreSQL**

```powershell
uv run --no-sync pytest packages/backend/tests/integration -v
uv run --no-sync alembic check
```

Expected: the integration suite and migration check pass. If local Docker/MinIO-specific tests report documented skips, record their names; do not call them passes.

- [ ] **Step 4: Review the final M2A diff and commit any mechanical formatting change**

```powershell
git diff --check 0184efc..HEAD
git diff --stat 0184efc..HEAD
git status --short
```

Expected: no whitespace errors; only the design, plan, regression, and store fix are new. If `ruff format` changed a tracked file, commit only that file with `git commit -m "style(sandbox): format cleanup race fix"`. Do not add local caches or ignored directories.

### Task 4: Rebuild M2B–M2D without touching M2E

**Files:**

- Rebase branch: `codex/m2b-skills`
- Rebase branch: `codex/m2c-tools-approvals`
- Rebase branch: `codex/m2d-memory`
- Preserve unchanged: `codex/m2e-child-agents` at `9a17eaf`

- [ ] **Step 1: Record safety anchors and create an isolated rebuild worktree**

From the repository root:

```powershell
git rev-parse codex/m2a-sandbox-race-fix codex/m2b-skills codex/m2c-tools-approvals codex/m2d-memory codex/m2e-child-agents
git worktree add .worktrees/codex-m2-stack-rebuild codex/m2b-skills
```

Expected old child tips are M2B `2129c2a`, M2C `1e60ee5`, M2D `aade0b3`; M2E must remain `9a17eaf`. Stop if these anchors differ unexpectedly.

- [ ] **Step 2: Rebase each child phase onto its corrected parent**

In `.worktrees/codex-m2-stack-rebuild`:

```powershell
git rebase --onto codex/m2a-sandbox-race-fix 0184efc codex/m2b-skills
git switch codex/m2c-tools-approvals
git rebase --onto codex/m2b-skills 2129c2a
git switch codex/m2d-memory
git rebase --onto codex/m2c-tools-approvals 1e60ee5
```

Expected: each rebase completes without dropping phase commits. If a conflict occurs, resolve only the overlapping phase files, run the focused tests for that phase, and inspect `git range-diff` before continuing.

- [ ] **Step 3: Move the local M2A branch name to the verified fix and prove the stack shape**

```powershell
git branch -f codex/m2a-goal-loop codex/m2a-sandbox-race-fix
git merge-base --is-ancestor codex/m2a-goal-loop codex/m2b-skills
git merge-base --is-ancestor codex/m2b-skills codex/m2c-tools-approvals
git merge-base --is-ancestor codex/m2c-tools-approvals codex/m2d-memory
git rev-parse codex/m2e-child-agents
git range-diff 0184efc..2129c2a codex/m2a-goal-loop..codex/m2b-skills
git range-diff 2129c2a..1e60ee5 codex/m2b-skills..codex/m2c-tools-approvals
git range-diff 1e60ee5..aade0b3 codex/m2c-tools-approvals..codex/m2d-memory
```

Expected: all three ancestry checks exit 0; all range-diffs show the same phase patches rebased to new commit IDs; M2E still resolves to `9a17eaf` and is not rebased.

- [ ] **Step 4: Update the four authorized remote branches atomically with explicit leases**

```powershell
git push --atomic origin `
  --force-with-lease=refs/heads/codex/m2a-goal-loop:0184efc `
  --force-with-lease=refs/heads/codex/m2b-skills:2129c2a `
  --force-with-lease=refs/heads/codex/m2c-tools-approvals:1e60ee5 `
  --force-with-lease=refs/heads/codex/m2d-memory:aade0b3 `
  codex/m2a-goal-loop:codex/m2a-goal-loop `
  codex/m2b-skills:codex/m2b-skills `
  codex/m2c-tools-approvals:codex/m2c-tools-approvals `
  codex/m2d-memory:codex/m2d-memory
```

Expected: all four refs update together. A lease rejection means the remote changed since inspection; do not weaken the lease or overwrite the new state.

### Task 5: Verify every rebuilt branch in CI

**Files:**

- Observe only: `.github/workflows/ci.yml`
- Do not create or modify pull requests.

- [ ] **Step 1: Find the new push-triggered run for each branch**

```powershell
$m2Branches = @(
  "codex/m2a-goal-loop",
  "codex/m2b-skills",
  "codex/m2c-tools-approvals",
  "codex/m2d-memory"
)
$m2Runs = foreach ($m2Branch in $m2Branches) {
  gh run list --workflow ci --branch $m2Branch --event push --limit 1 `
    --json databaseId,headBranch,headSha,status,conclusion,url | ConvertFrom-Json
}
$m2Runs | Format-Table databaseId,headBranch,headSha,status,conclusion,url
```

Expected: every `headSha` equals its rebuilt local branch tip and every run is newer than the cancelled/failed historical runs.

- [ ] **Step 2: Watch all four runs to terminal conclusions**

```powershell
foreach ($m2Run in $m2Runs) {
  gh run watch $m2Run.databaseId --exit-status
  gh run view $m2Run.databaseId --json status,conclusion,jobs,url
}
```

Expected: `backend-unit`, `backend-integration`, `web`, and `compose-e2e` all succeed on all four branches. If any job fails, inspect its failing step and logs; do not infer success from another branch or rerun a failing tree without explaining the failure.

- [ ] **Step 3: Report evidence and preserve the agreed boundaries**

Report the four new branch tips, four CI run URLs, and every job conclusion. Explicitly state that M2E was not pushed, no PR was created, PR #8 was untouched, and `feat/goal-loop` was not deleted. Remove integration environment variables from the current PowerShell process when finished:

```powershell
Remove-Item Env:TEST_DATABASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
```
