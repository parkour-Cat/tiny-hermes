# M1 §24.1 official run on a 16 GB host — 2026-08-15

## 1. Scope

`scripts/benchmark_m1.py` on a Linux 8 vCPU / 16 GB host, with every image
built from the checked-out tree. No `--seconds`, no `--gate` filter, no
`allow_skip`. Thresholds were not edited. The official JSON is
`passed: true` and the process exited 0.

This run exists to close the two holes the 30 GiB run left open
(`2026-08-14-m1-benchmark-live-4.md` §5):

- That run measured a host with roughly twice the reference RAM. More RAM
  than the reference is a tailwind, not a neutral difference: it hides disk
  behind page cache.
- That run measured containers with patches copied into them by hand. What
  was measured was not provably what was committed.

Host address, passwords, and login material are absent from this file.

This record does **not** mark `0.1 Technical Preview`. It does **not**
claim CI is green. Disk is still `vda` `ROTA=1` (not claimed as local SSD).

## 2. Why a 16 GB host could not run this before

`2026-08-14-m1-reference-host.md` recorded a 16 GB machine whose MemTotal
is `15850224 kB` ≈ 15.12 GiB. `matches_reference` rounded that to 15 whole
GiB and refused the host, so the §24.1 run moved to a 30 GiB machine it
never needed. The rounding was added by `8769a66` for exactly this case,
but `int(15.12 + 0.5)` is 15, and its test used 15.64 GiB, which rounds up.

`8bbd8cb` compares MemTotal against an explicit 15.0 GiB floor instead.
Nothing sold as 8 or 12 GB reaches that floor, so the reference shape still
means 8 vCPU / 16 GB. The §24.1 thresholds themselves are untouched.

This host reports the same `15850224 kB`. It is the shape that was refused.

## 3. Host

| Fact | Measured |
|---|---|
| OS | Linux 7.0.0-14-generic, Ubuntu 26.04 LTS |
| vCPU | 8 (`nproc`) |
| MemTotal | `15850224 kB` ≈ **15.116 GiB** |
| Disk | `vda` 50 GiB, `lsblk` `ROTA=1` (virtio; not claimed as local SSD) |
| Docker | 29.1.3, Compose 2.40.3 (Ubuntu packages) |
| Python | CPython 3.12.13 under uv 0.11.26 |

## 4. What ran

Tree: `a70d377` on `bench/ram-tolerance`, `git status --short` empty. The
clone was created from a `git bundle` copied to the host, so the machine
holds no repository credential and its tree is the bundled history.

```
docker build -t tiny-hermes-sandbox:ci -f deploy/sandbox/Dockerfile deploy/sandbox
docker compose -f deploy/compose/compose.yaml down -v --remove-orphans
docker compose -f deploy/compose/compose.yaml up -d --build --wait
uv run --no-sync python scripts/benchmark_m1.py --shape-only
uv run --no-sync python scripts/benchmark_m1.py
```

Sandbox image `sha256:0cebff61518c…`. API, Worker, Scheduler, Controller,
migrate and Web were all built by that `--build`, from empty volumes
(`down -v` first). **Nothing was copied into a running container.** The
stack used the Compose file's default interpolations, which is what the
`compose-e2e` CI job does; a generated-secret stack is still unproven.

`--shape-only` exited **0** with `shape_ok: true` — the gate that exited 2
on this shape yesterday.

Official run: 03:36:33Z–04:01:01Z UTC (24.5 min), `DETERMINISTIC_MODEL_DELAY_MS`
default 50, sandbox image cached before the first cold-start sample. Script
RSS 18.5 MiB.

## 5. Official gate table

| Gate | passed | p50 | p95 | gate | 30 GiB run 4 p95 |
|---|---|---:|---:|---:|---:|
| create_run | true | 22.6 ms | **30.2 ms** | 300 ms, err < 0.001 | 32.3 ms |
| run_event | true | 1.17 ms | **1.54 ms** | 100 ms, lost 0 | 1.64 ms |
| sse | true | 122 events | 122 events | 5 s cadence | 122 events |
| sandbox_cold | true | 456 ms | **481 ms** | 3000 ms | 488 ms |
| sandbox_warm | true | 0.25 ms | **19.3 ms** | 300 ms | 0.36 ms |
| workspace_small | true | 637 ms | **677 ms** | 1000 ms | 732 ms |
| workspace_large | true | 2.38 s | **2.38 s** | 15 s | 2.43 s |
| next_run | true | 113 ms | **120 ms** | 3 s | 120 ms |
| worker_recovery | true | — | **21.07 s** | 30 s | 20.96 s |
| service_recovery | true | — | **6.61 s** | 60 s | 6.70 s |

`create_run` error rate 0.000167 (1 of 6001), gate 0.001.

Cell lines from stderr:

```
create_run n=6001 sessions=200 300.0s
run_event n=150001 lost=0 300.0s
sse connections=500 600.4s reasons=0 missed_cadence=0 worst_hold_gap=0.0s
sandbox_cold n=12 errors=0
sandbox_warm n=5 reasons=0
worker_recovery queued_in=21.07s
service_recovery elapsed=6.61s
```

Nine of the ten cells came out at or below their 30 GiB values. The extra
RAM bought nothing measurable. `sandbox_warm` p95 is the one that moved
(0.36 ms to 19.3 ms) on a 5-sample cell; it is 6% of its 300 ms gate, and
the container id did not change on any of the five reacquisitions.

## 6. Raw official JSON

```json
{
  "git_sha": "a70d37704090527660a6970f39e5f766656d662f",
  "shape": {
    "os": "linux",
    "vcpu": 8,
    "ram_gib": 15.115951538085938
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
  "rss_mib": 18.4765625,
  "gates": {
    "create_run": {
      "name": "create_run",
      "status": "measured",
      "passed": true,
      "p50_ms": 22.62176649992398,
      "p95_ms": 30.159644800062324,
      "p99_ms": 35.07575418987472,
      "error_rate": 0.00016663889351774705,
      "reasons": [],
      "gate": {"p95_ms": 300.0, "error_rate": 0.001, "max_loss": null, "max_s": null}
    },
    "run_event": {
      "name": "run_event",
      "status": "measured",
      "passed": true,
      "p50_ms": 1.1729440000181057,
      "p95_ms": 1.538054999855376,
      "p99_ms": 1.9849049999720592,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {"p95_ms": 100.0, "error_rate": null, "max_loss": 0, "max_s": null}
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
      "gate": {"p95_ms": null, "error_rate": null, "max_loss": null, "max_s": null}
    },
    "sandbox_cold": {
      "name": "sandbox_cold",
      "status": "measured",
      "passed": true,
      "p50_ms": 455.9140290000414,
      "p95_ms": 480.9042617998557,
      "p99_ms": 481.22847315971285,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {"p95_ms": 3000.0, "error_rate": null, "max_loss": null, "max_s": null}
    },
    "sandbox_warm": {
      "name": "sandbox_warm",
      "status": "measured",
      "passed": true,
      "p50_ms": 0.24602599978607032,
      "p95_ms": 19.276100199658686,
      "p99_ms": 20.977248039653205,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {"p95_ms": 300.0, "error_rate": null, "max_loss": null, "max_s": null}
    },
    "workspace_small": {
      "name": "workspace_small",
      "status": "measured",
      "passed": true,
      "p50_ms": 637.1672204998049,
      "p95_ms": 676.6567634997273,
      "p99_ms": 678.4347110997669,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {"p95_ms": 1000.0, "error_rate": null, "max_loss": null, "max_s": null}
    },
    "workspace_large": {
      "name": "workspace_large",
      "status": "measured",
      "passed": true,
      "p50_ms": 2379.6474529999614,
      "p95_ms": 2379.6474529999614,
      "p99_ms": 2379.6474529999614,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {"p95_ms": 15000.0, "error_rate": null, "max_loss": null, "max_s": null}
    },
    "next_run": {
      "name": "next_run",
      "status": "measured",
      "passed": true,
      "p50_ms": 112.90730400014581,
      "p95_ms": 119.98324900014268,
      "p99_ms": 121.03754420022597,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {"p95_ms": 3000.0, "error_rate": null, "max_loss": null, "max_s": null}
    },
    "worker_recovery": {
      "name": "worker_recovery",
      "status": "measured",
      "passed": true,
      "p50_ms": 21074.550652999733,
      "p95_ms": 21074.550652999733,
      "p99_ms": 21074.550652999733,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {"p95_ms": null, "error_rate": null, "max_loss": null, "max_s": 30.0}
    },
    "service_recovery": {
      "name": "service_recovery",
      "status": "measured",
      "passed": true,
      "p50_ms": 6607.451819999824,
      "p95_ms": 6607.451819999824,
      "p99_ms": 6607.451819999824,
      "error_rate": 0.0,
      "reasons": [],
      "gate": {"p95_ms": null, "error_rate": null, "max_loss": null, "max_s": 60.0}
    }
  },
  "passed": true
}
```

## 7. Explicitly not claimed

- `0.1 Technical Preview` (`pyproject.toml` description is unchanged).
- CI green. GitHub Actions still refuses to start jobs on this private
  repository for account spending reasons, so no check has run on this
  branch or on any of the phase-4 pull requests.
- Local SSD. The disk is a virtio device reporting `ROTA=1`. §24.1's
  reference environment says local SSD; this run does not meet that word.
- A generated-secret Compose stack. This run used the Compose file's
  default interpolations, as `compose-e2e` does.
- Playwright, the restart drill, and the workspace drill on this host.
  Only the benchmark ran here.
- That the branch is merged. `bench/ram-tolerance` sits on top of
  `cursor/m1-benchmark-drivers-c232` (PR #10), which is itself the top of an
  unmerged six-branch stack.
