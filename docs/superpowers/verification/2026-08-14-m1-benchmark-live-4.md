# M1 §24.1 live-driver run 4 — 2026-08-14

## 1. Scope

Fourth official `scripts/benchmark_m1.py` on the same 8 vCPU Linux host.
No `--seconds`, no `--gate` filter, no `allow_skip`. Host address,
passwords, tokens, and cookie material are absent from this file.

The official JSON is `passed: true` and the process exited 0. Thresholds
were not edited.

This record does **not** mark `0.1 Technical Preview`. It does **not**
claim CI is green. Disk is still `vda` `ROTA=1` (not claimed as local SSD).

## 2. What ran

Host tree was still `cursor/m1-reference-host-c232` at
`d244a86edd9f5076b5595c24f828c9346980c7ba`. That is the `git_sha` in the
JSON. Live scripts and the platform patches below were copied from
`cursor/m1-benchmark-drivers-c232` at `704b05d` into the running
API / Worker / Scheduler (no image rebuild).

Patches in the measured process:

- `EventStreamHub`: one live poll per Run, fan-out to every SSE subscriber
  (`f6608d3`). Catch-up stays per cursor.
- Default `worker_lease_seconds=20` and `scheduler_interval_seconds=1`
  (`704b05d`). The lease is not a §24.1 number; kill-to-queued must fit
  in 30 s.
- Earlier on this host, already in the containers: `apply_signal` releases
  WorkerLease when the Run leaves `RUNNING` (`39fcfcd`); API pool 80+40;
  Postgres `max_connections=200`.

A 40 s SSE probe (cannot pass duration) immediately before this run:
`missed_cadence=0`, `worst_hold_gap=0.0s`. Targeted `worker_recovery` on
the same process: queued in 20.27 s.

Wall: 18:28:54Z–18:53:18Z UTC (~24 min). `shape_ok`: **true**.
`DETERMINISTIC_MODEL_DELAY_MS=50`. Sandbox image already cached.
Script RSS 18.2 MiB.

## 3. Official gate table

| Gate | passed | p50 | p95 | notes |
|---|---|---:|---:|---|
| create_run | **true** | 24.5 ms | 32.3 ms | 6001 POST / 200 Session / 300.0 s; error_rate 0.000167 (gate 0.001) |
| run_event | **true** | 1.23 ms | 1.64 ms | 150000 writes, lost=0, 500/s, 300.0 s |
| sse | **true** | 122 events | 122 events | 500 connections, 600.5 s; `missed_cadence=0`; no reconnect gap |
| sandbox_cold | **true** | 477 ms | 488 ms | 12/12; gate 3000 ms |
| sandbox_warm | **true** | 0.22 ms | 0.36 ms | 5/5; reasons=0; same-container warm after the lease-release fix |
| workspace_small | **true** | 639 ms | 732 ms | gate 1000 ms |
| workspace_large | **true** | 2.43 s | 2.43 s | gate 15 s |
| next_run | **true** | 117 ms | 120 ms | gate 3 s |
| worker_recovery | **true** | — | 20.96 s | gate 30 s |
| service_recovery | **true** | — | 6.70 s | gate 60 s |

stderr lines that name the cells:

```
create_run n=6001 sessions=200 300.0s
run_event n=150000 lost=0 300.0s
sse connections=500 600.5s reasons=0 missed_cadence=0 worst_hold_gap=0.0s
sandbox_cold n=12 errors=0
sandbox_warm n=5 reasons=0
worker_recovery queued_in=20.96s
service_recovery elapsed=6.70s
```

## 4. Raw official JSON

```json
{
  "git_sha": "d244a86edd9f5076b5595c24f828c9346980c7ba",
  "shape": {
    "os": "linux",
    "vcpu": 8,
    "ram_gib": 30.47140884399414
  },
  "reference_shape": {
    "os": "linux",
    "vcpu": 8,
    "ram_gib": 16
  },
  "shape_ok": true,
  "deterministic_delay_ms": 50,
  "preload_run_events": 100000,
  "cpu_percent": 0.0,
  "rss_mib": 18.20703125,
  "gates": {
    "create_run": {
      "name": "create_run",
      "status": "measured",
      "passed": true,
      "p50_ms": 24.478298999383696,
      "p95_ms": 32.32110495118832,
      "p99_ms": 36.63425637943876,
      "error_rate": 0.00016663889351774705,
      "reasons": [],
      "gate": {
        "p95_ms": 300.0,
        "error_rate": 0.001,
        "max_loss": null,
        "max_s": null
      }
    },
    "run_event": {
      "name": "run_event",
      "status": "measured",
      "passed": true,
      "p50_ms": 1.22532349996618,
      "p95_ms": 1.6377719473894097,
      "p99_ms": 2.6211575893466965,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {
        "p95_ms": 100.0,
        "error_rate": null,
        "max_loss": 0,
        "max_s": null
      }
    },
    "sse": {
      "name": "sse",
      "status": "measured",
      "passed": true,
      "p50_ms": 122.0,
      "p95_ms": 122.0,
      "p99_ms": 122.0,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {
        "p95_ms": null,
        "error_rate": null,
        "max_loss": null,
        "max_s": null
      }
    },
    "sandbox_cold": {
      "name": "sandbox_cold",
      "status": "measured",
      "passed": true,
      "p50_ms": 477.2465474998171,
      "p95_ms": 488.0696580004951,
      "p99_ms": 489.78330840040144,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {
        "p95_ms": 3000.0,
        "error_rate": null,
        "max_loss": null,
        "max_s": null
      }
    },
    "sandbox_warm": {
      "name": "sandbox_warm",
      "status": "measured",
      "passed": true,
      "p50_ms": 0.22124400129541755,
      "p95_ms": 0.3611545980675146,
      "p99_ms": 0.38282291789073497,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {
        "p95_ms": 300.0,
        "error_rate": null,
        "max_loss": null,
        "max_s": null
      }
    },
    "workspace_small": {
      "name": "workspace_small",
      "status": "measured",
      "passed": true,
      "p50_ms": 638.6916225019377,
      "p95_ms": 731.54767269898,
      "p99_ms": 734.0393601372853,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {
        "p95_ms": 1000.0,
        "error_rate": null,
        "max_loss": null,
        "max_s": null
      }
    },
    "workspace_large": {
      "name": "workspace_large",
      "status": "measured",
      "passed": true,
      "p50_ms": 2429.2167549974693,
      "p95_ms": 2429.2167549974693,
      "p99_ms": 2429.2167549974693,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {
        "p95_ms": 15000.0,
        "error_rate": null,
        "max_loss": null,
        "max_s": null
      }
    },
    "next_run": {
      "name": "next_run",
      "status": "measured",
      "passed": true,
      "p50_ms": 116.61426700084121,
      "p95_ms": 120.29803859986714,
      "p99_ms": 120.95711491987458,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {
        "p95_ms": 3000.0,
        "error_rate": null,
        "max_loss": null,
        "max_s": null
      }
    },
    "worker_recovery": {
      "name": "worker_recovery",
      "status": "measured",
      "passed": true,
      "p50_ms": 20956.047532999946,
      "p95_ms": 20956.047532999946,
      "p99_ms": 20956.047532999946,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {
        "p95_ms": null,
        "error_rate": null,
        "max_loss": null,
        "max_s": 30.0
      }
    },
    "service_recovery": {
      "name": "service_recovery",
      "status": "measured",
      "passed": true,
      "p50_ms": 6700.490863000596,
      "p95_ms": 6700.490863000596,
      "p99_ms": 6700.490863000596,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {
        "p95_ms": null,
        "error_rate": null,
        "max_loss": null,
        "max_s": 60.0
      }
    }
  },
  "passed": true
}
```

## 5. Explicitly not claimed

- `0.1 Technical Preview` (`pyproject.toml` description is unchanged).
- CI green.
- Local SSD.
- That the host git tree at `d244a86` already contains the patches; they
  were copied into the running containers for this measurement.
