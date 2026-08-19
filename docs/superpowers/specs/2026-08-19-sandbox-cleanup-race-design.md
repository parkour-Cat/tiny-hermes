# Sandbox Cleanup Race Design

## Problem

M2B CI run `32216788614` exposed a race between the Worker and Scheduler while an over-quota workspace is rolled back.

The observed order was:

1. The Scheduler selected an old sandbox reservation for cleanup.
2. The Worker completed rollback cleanup, released that reservation, and moved the Run to `paused(limit)`.
3. The Run was resumed and received a new live lease.
4. The Scheduler tried to clean up its stale selection. The Controller correctly refused with `lease_still_live` because the resumed Run now held a lease.
5. The Scheduler's error path unconditionally changed the old, already released reservation back to `isolated`.
6. Each resumed slice then saw `already_reserved`, ended without work, and retried until the drill timed out after 180 seconds.

The defect is shared by M2A through M2D. M2C and M2D passed their drills because they did not encounter this ordering, not because later code removed the race.

## Required Invariant

A released sandbox reservation is terminal. No stale cleanup attempt may move it back into a live state.

Cleanup remains fail-closed for reservations that may still own a container: an active, kept, or already isolated reservation whose cleanup cannot be confirmed must remain isolated. Only a reservation that another actor has already released is left released.

## Chosen Approach

Guard the transition in `SqlSandboxStore`, where the reservation row is owned.

The isolation operation will lock and re-read the current reservation row before changing it. It will change only a live reservation to `isolated`. If another transaction has already released the row, it will return the released reservation unchanged. Holding the row lock makes the check and update one database operation from other writers' point of view.

The Scheduler will continue calling the same isolation operation after an unconfirmed cleanup. It does not need to infer why cleanup failed or special-case Controller error strings. The store enforces the state invariant for every caller.

This is preferred over:

- re-reading in the Scheduler without a lock, which leaves another check/write race;
- treating `lease_still_live` as success, which could forget a real unconfirmed container;
- retrying indefinitely, which preserves the livelock seen in CI.

## Data Flow

On a cleanup exception:

1. The Scheduler opens a database transaction.
2. `SqlSandboxStore.isolate()` locks the reservation row.
3. If the current status is live, the store records `isolated` and `cleanup_unconfirmed`.
4. If the current status is `released`, the store makes no change.
5. The transaction commits and later scans see the true current owner state.

This keeps the Controller's lease check unchanged. A Controller refusal remains meaningful evidence that cleanup was not authorized at that moment; it just cannot resurrect state that a different successful cleanup already closed.

## Testing

Add a deterministic integration regression in `packages/backend/tests/integration/runs/test_scheduler_sandboxes.py`:

1. Create an expired kept reservation so the Scheduler selects it.
2. During the stand-in Controller's cleanup call, release the reservation through a separate database transaction, then raise a cleanup error.
3. Let the Scheduler execute its existing error path.
4. Assert the reservation remains `released`, not `isolated`.

The test must fail against the current M2A code by observing `isolated`. After the store change it must pass. Existing tests that require a genuinely unconfirmed live reservation to become and remain `isolated` must continue passing.

Verification will include the targeted regression, the scheduler sandbox test module, backend unit tests, Ruff, Pyright, and the repository's CI jobs on each rebuilt stacked branch.

## Branch Rollout

The fix and its documentation are added on top of M2A. M2B, M2C, and M2D are then rebased in order onto the corrected parent and updated with `--force-with-lease`. M2E remains local and unchanged. No pull request is created.

The cancelled M2A run and failed M2B run are historical evidence only. Each updated branch must produce a new CI run; success is not inferred from M2C or M2D's earlier green runs.

## Known Local Baseline Limitation

In a Windows Git worktree, the existing benchmark unit test reads `.git/HEAD` as though `.git` were a directory. The baseline therefore has 786 passing tests and one unrelated failure, while Ruff and Pyright pass. This existing limitation is recorded but is outside this race fix; CI on Linux remains the full verification authority for the complete unit suite.

## Non-goals

- Changing sandbox lease rules or Controller authorization.
- Relaxing fail-closed cleanup behavior.
- Fixing the unrelated Windows worktree benchmark test.
- Pushing M2E or creating pull requests.
