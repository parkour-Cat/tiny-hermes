# The lease-ownership fix, and the §24.1 run that carries it — 2026-08-16

## 1. Scope

`fix/lease-ownership` (`624fb7d`), branched from `bench/ram-tolerance`. It
repairs the failure `2026-08-15-m1-drills-16gb.md` §3 recorded: a 100 MiB
checkpoint dies under the 20 s lease that §24.1's recovery cell requires.

Same 16 GB host, same tree-built images. Host address, passwords and tokens
are absent from this file.

This record does **not** mark `0.1 Technical Preview` and does **not** claim
CI is green — GitHub Actions still refuses to start jobs on this repository.

## 2. What was wrong

`record_slice` fenced every write with three conditions: the lease row must
exist under the caller's `lease_id`, must not be released, and its
**version** must equal what the caller presented.

That version is incremented by the holder's own renewal task. A round builds
its slice command before the work and records it after, so any round that
outlives one renewal interval presents a version its own heartbeat has
already moved past. `record_slice` raised `LeaseLost`, and
`_checkpoint_round` swallowed that exception with no log and no event. The
slice ended having written no checkpoint and released no lease; the Run sat
in `RUNNING` until the Scheduler expired it, was interrupted, retried, and
failed after four attempts.

The proof, from an instrumented Worker, is four identical lines whose
`command_lease_version` is exactly one behind `handle_version_now`:

```
CKPTDBG swallowed LeaseLost command_lease_version=1 handle_version_now=2
CKPTDBG swallowed LeaseLost command_lease_version=3 handle_version_now=4
CKPTDBG swallowed LeaseLost command_lease_version=5 handle_version_now=6
CKPTDBG swallowed LeaseLost command_lease_version=7 handle_version_now=8
```

A 100 MiB checkpoint takes about 8 s and a 20 s lease renews every 6.7 s, so
it failed every time. **The 30 s default did not prevent this; it hid it.**
Its first renewal falls at 10 s, past the end of an 8 s round. A slower host,
a larger workspace, or a busier disk reaches the same interval.

## 3. The fix

Ownership is the lease id and `released_at`, which already fence a Worker
that lost the Run: `claim` upserts `id=uuid4()`, so a re-claim invalidates
every id a previous holder still carries, and a Scheduler reclaim sets
`released_at`. The version never carried ownership, so
`expected_lease_version` is deleted from `RecordSliceCommand` rather than
left in place looking like a guard.

Two existing tests encoded the old rule and were rewritten to the real one:

- `test_a_stale_lease_version_is_refused_and_changes_nothing` became
  `test_a_lease_that_no_longer_owns_the_run_is_refused`, which replaces the
  row's id and expects `LeaseLost`.
- `test_commit_transaction_is_atomic_across_all_five_tables` poisoned its
  transaction with a stale version; it now poisons it with a lease id that
  no longer owns the Run. What it proves is unchanged.

`test_a_round_that_outlived_its_own_renewal_still_records` is new: renew,
then record with the pre-renewal command, and the write must land.

## 4. Verification

| Check | Result |
|---|---|
| Backend unit | **671 passed** |
| Backend integration (`tiny_hermes_test`) | **399 passed** |
| `workspace_drill.py`, 20 s lease | **PASS** — large commit `files=501 bytes=~100MiB took=8.4s`, leftovers 0 |
| `workspace_drill.py --phase quota` | **PASS** |
| `restart_drill.py` | **PASS**, all four scenarios held, 124.0 s |
| **§24.1, all ten cells** | **`passed: true`, exit 0** |

The same drill failed three times out of three on the parent branch at the
same lease, and passes here.

## 5. The §24.1 table on the fixed tree

`git_sha` `624fb7d40efc96032e1b55b6c21df322871023b1`, shape
`15.116 GiB` / 8 vCPU, `shape_ok: true`, script RSS 18.5 MiB, 15:48:43Z–16:13:08Z.

| Gate | passed | p50 | p95 | gate | 2026-08-15 run |
|---|---|---:|---:|---:|---:|
| create_run | true | 22.9 ms | 30.7 ms | 300 ms | 30.2 ms |
| run_event | true | 1.19 ms | 1.53 ms | 100 ms | 1.54 ms |
| sse | true | 122 events | 122 events | 5 s cadence | 122 events |
| sandbox_cold | true | 476 ms | 514 ms | 3000 ms | 481 ms |
| sandbox_warm | true | 21.2 ms | 29.8 ms | 300 ms | 19.3 ms |
| workspace_small | true | 634 ms | 677 ms | 1000 ms | 677 ms |
| workspace_large | true | 2.38 s | 2.38 s | 15 s | 2.38 s |
| next_run | true | 113 ms | 117 ms | 3 s | 120 ms |
| worker_recovery | true | — | 21.70 s | 30 s | 20.96 s |
| service_recovery | true | — | 6.67 s | 60 s | 6.61 s |

Every gate has error rate 0.0, including `create_run` (6000 of 6000).

```
create_run n=6000 sessions=200 300.0s
run_event n=150000 lost=0 300.0s
sse connections=500 600.4s reasons=0 missed_cadence=0 worst_hold_gap=0.0s
sandbox_cold n=12 errors=0
sandbox_warm n=5 reasons=0
worker_recovery queued_in=21.70s
service_recovery elapsed=6.67s
```

`worker_recovery` stays inside 30 s, which is the cell the 20 s lease was
lowered for. The lease no longer has to be traded against long checkpoints:
both hold on the same tree.

## 6. Explicitly not claimed

- `0.1 Technical Preview`.
- CI green. No check has run on this branch; the runner is refused for
  account spending reasons.
- Local SSD. The disk is virtio, `ROTA=1`.
- Playwright on this branch. It passed on the parent tree
  (`2026-08-15-m1-drills-16gb.md`); the fix does not touch the console, but
  it was not re-run here.
- A generated-secret Compose stack.
- That the branch is merged, or reviewed by anyone.
