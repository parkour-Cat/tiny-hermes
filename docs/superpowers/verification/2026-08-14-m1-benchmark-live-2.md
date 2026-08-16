# M1 §24.1 live-driver run 2 — 2026-08-14

## 1. Scope

Second official `scripts/benchmark_m1.py` on the same 8 vCPU Linux host
after the driver measurement fixes in `47d4bc4`. Host address, passwords,
tokens, and cookie material are absent from this file.

This record is the raw official JSON. It does **not** mark `0.1 Technical
Preview`. It does **not** claim product design §24.1 passed.

Platform tree on the host was still `cursor/m1-reference-host-c232` at
`d244a86edd9f5076b5595c24f828c9346980c7ba`. Live scripts were copied from
`cursor/m1-benchmark-drivers-c232` at `47d4bc4`. `git_sha` in the JSON is
the host tree. The later SSE hold-window commit `24ff1c1` was **not** in
this process.

## 2. Host

Same machine as `2026-08-14-m1-benchmark-live.md`: Linux, 8 vCPU,
MemTotal ≈ 30.47 GiB, `vda` `ROTA=1` (not claimed as local SSD),
`DETERMINISTIC_MODEL_DELAY_MS=50`, sandbox image already cached.

`shape_ok`: **true**. `passed`: **false**. Exit 1. Wall ~27 min
(17:22:19Z–17:49:15Z UTC). Script RSS 18.2 MiB.

## 3. Official gate table (raw)

| Gate | passed | p50 | p95 | notes |
|---|---|---:|---:|---|
| create_run | **true** | 22.5 ms | 29.8 ms | 6001 POST / 200 Session / 300.0 s; error_rate 0 |
| run_event | **true** | 1.17 ms | 1.61 ms | 150000 writes, lost=0, 500/s, 300.0 s. The 0.5 s duration slack removed the false `300.0<300` fail |
| sse | false | — | — | 500 connections, 608.9 s. All 500 “missed the 5s cadence”. No reconnect-gap reason. Driver still scored the attach/history gap (`skip_first=1` only) |
| sandbox_cold | **true** | 468 ms | 481 ms | 12/12; gate 3000 ms |
| sandbox_warm | false | 26.09 s | 26.09 s | 5/5 samples; no container-id reason. See §4 |
| workspace_small | **true** | 577 ms | 584 ms | gate 1000 ms |
| workspace_large | **true** | 2.46 s | 2.46 s | gate 15 s |
| next_run | **true** | 106 ms | 112 ms | gate 3 s |
| worker_recovery | false | — | — | queued in **31.26 s** (gate 30 s). Scheduler was not restarted |
| service_recovery | **true** | — | 6.51 s | gate 60 s. Leftover recovery Run was cancelled |

## 4. What the numbers are (not threshold edits)

1. **run_event** now passes. Same write volume as run 1; only the clock
   compare changed.
2. **service_recovery** now passes. Cancelling the `sleep 90` Run after
   worker_recovery left the Worker free for the new task.
3. **sandbox_warm** is still ~26 s, but that is no longer leftover
   `sleep 35`. Four warm Runs show the same event gap: `run_slice_ended`
   then **26.07 s** of nothing, then `run_lease_acquired` and
   `run_completed` in **~170 ms**. The thaw itself is inside the 300 ms
   cell. The wait is the committed-checkpoint path: `record_slice(signal=None)`
   keeps the WorkerLease, then `apply_signal(SLICE_ENDED)` queued the Run
   **without releasing the lease**, so the next claim waited out the
   remaining ~26 s of a 30 s lease. That is a store bug, not a §24.1 edit.
4. **worker_recovery** 31.26 s is lease 30 s plus ~1.3 s of Scheduler
   reclaim after `compose kill`. No migrate/restart of the Scheduler this
   time (run 1 was 34.72 s).
5. **sse** still fails on this JSON because this process did not include
   `24ff1c1` (score cadence only after the hold starts).

The product cells were not edited.

## 5. Explicitly not claimed

- Product §24.1 passed.
- `0.1 Technical Preview`.
- Local SSD.
- Isolated 300 ms warm thaw as a passing cell (the thaw after the second
  lease is ~170 ms; the official p95 includes the leaked-lease wait).
- SSE 5 s cadence on 500 connections.
