# M1 §24.1 live-driver run — 2026-08-14

## 1. Scope

An operator Linux spot instance ran `scripts/benchmark_m1.py` after the
ten live drivers in `scripts/benchmark_live.py` were written. This
record is the raw official JSON. It does **not** mark `0.1 Technical
Preview`. It does **not** claim product design §24.1 passed.

Host address, passwords, tokens, and cookie material are absent from
this file.

Platform tree on the host was `cursor/m1-reference-host-c232` at
`d244a86edd9f5076b5595c24f828c9346980c7ba`. The live scripts were
copied from `cursor/m1-benchmark-drivers-c232` at `83c0115` (loader
fix) on top of that tree. `git_sha` in the JSON is therefore the host
tree, not the script commit.

## 2. Host

| Fact | Measured |
|---|---|
| OS | Linux 7.0.0-14-generic, Ubuntu 26.04 |
| vCPU | 8 (`nproc`) |
| MemTotal | 31951588 kB ≈ **30.47 GiB** |
| Disk | `vda` 50 GiB, `lsblk` `ROTA=1` (virtio; not claimed as local SSD) |
| Docker | Compose 2.40.3; sandbox image `tiny-hermes-sandbox:ci` already cached |
| Model delay | `DETERMINISTIC_MODEL_DELAY_MS=50` |
| Script RSS | 18.2 MiB |

`shape_ok`: **true**. `passed`: **false**. Exit 1.

## 3. Official gate table (raw)

| Gate | passed | p50 | p95 | notes |
|---|---|---:|---:|---|
| create_run | **true** | 22.5 ms | 29.9 ms | 6001 POSTs / 200 sessions / 300.0 s; error_rate 0 |
| run_event | false | 1.16 ms | 1.55 ms | 150000 writes, lost=0, 500/s. Only reason: `sampled 300.0s, gate requires 300s` (clock compare; see §4) |
| sse | false | — | — | 500 connections, 603.9 s. 498 connections missed the 5 s cadence. No reconnect-gap reason |
| sandbox_cold | **true** | 471 ms | 477 ms | 12/12 containers; gate 3000 ms |
| sandbox_warm | false | 26.2 s | 26.2 s | p95 far above 300 ms. Five “container id changed” reasons were 12-char `docker ps` prefixes of the same inspect id |
| workspace_small | **true** | 576 ms | 583 ms | gate 1000 ms |
| workspace_large | **true** | 2.34 s | 2.34 s | 1000 files / ~100 MiB; gate 15 s |
| next_run | **true** | 107 ms | 1.72 s | gate 3 s |
| worker_recovery | false | — | — | queued in **34.72 s** (gate 30 s) |
| service_recovery | false | — | — | **66.39 s** (gate 60 s); new Run stayed `queued` for 60 s after API restart |

## 4. Script defects seen on this run (not threshold edits)

These are bugs in the driver/evaluator, recorded so a later run is not
compared blindly to this JSON:

1. `evaluate` treated `sampled_s=299.96…` printed as `300.0` as short of
   `duration_s=300`. run_event’s writes already met p95 / rate / loss.
2. Warm container comparison used a short id against a full id of the
   **same** container.
3. `restart_drill.compose` printed to stdout, so the official document
   was not strict JSON until the first `{`.

The product cells were not edited. After this record the three defects
above are fixed in the driver branch. That does not turn this JSON into
a pass.

## 5. Explicitly not claimed

- Product §24.1 passed.
- `0.1 Technical Preview`.
- Local SSD.
- Isolated 300 ms warm thaw (the driver timed freeze→running, ~26 s).
- SSE 5 s cadence on 500 connections.

The Compose stack was **stopped** after the run.
