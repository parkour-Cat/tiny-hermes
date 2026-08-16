# M1 §24.1 targeted re-run — 2026-08-14

## 1. Scope

After run 2, three cells were re-measured on the same 8 vCPU host with
`apply_signal` releasing WorkerLease (`39fcfcd` copied into the running
Worker) and the SSE hold-window driver (`24ff1c1`). This is **not** a
full ten-gate official document. `report.passed` on a three-gate subset
must not be read as a §24.1 pass.

Host address and credentials are absent. Thresholds were not edited.

## 2. Results

| Gate | passed | p50 | p95 | notes |
|---|---|---:|---:|---|
| sse | false | 122 events | 123 events | 500 connections, 602.2 s. 499 “missed the 5s cadence”. No reconnect gap. Process did not yet have the asyncio subscriber or the API pool change |
| sandbox_warm | **true** | 10.7 ms | **27.6 ms** | gate 300 ms. Same container. The 26 s wait was the leaked lease |
| worker_recovery | false | — | — | queued in **31.57 s** (gate 30 s) |

`shape_ok`: true. Targeted script exit 1.

## 3. Follow-up probes (SSE only, `--seconds 40`, cannot pass duration)

These are diagnostic. A 40 s sample is shorter than the 600 s cell.

| Driver / API | missed_cadence | worst hold gap |
|---|---:|---:|
| Hold-window + anchor wait | 142 / 500 | 11.0 s |
| Same + API pool 80+40, Postgres `max_connections=200` | 262 / 500 | 11.7 s |
| Same + 500 async subscribers on one loop | 306 / 500 | 13.4 s |

Connections receive about the right *count* of events. Gaps of 11–13 s
are the 500 independent 0.5 s DB polls fighting one another, not a
missing write. That is still a failed 5 s cadence cell.

## 4. worker_recovery

`compose kill worker` after the Run is `running`. Lease default 30 s,
Scheduler interval 5 s, reclaim observed at 31.3–31.6 s on two tries.
The cell and the lease are the same number; expiry plus one scan does
not fit in 30 s. Thresholds were not lowered.

## 5. Explicitly not claimed

- Product §24.1 passed.
- `0.1 Technical Preview`.
- A three-gate JSON with `passed: true` (this one is `passed: false`).
