# M1 reference-host run — 2026-08-14

## 1. Scope

An operator provided a Linux spot instance so slice 4D could leave the
4 vCPU Cloud Agent host. This record is what that machine actually
produced. It does **not** mark `0.1 Technical Preview`. It does **not**
claim product design §24.1 passed.

Code under test: `cursor/phase-4d-acceptance-c232` at
`1a5ff96314f2f65c29093f12184fc282f383c7df`. Secrets, cookies, tokens,
passwords, the host address, and login material are absent from this
file.

## 2. Host

| Fact | Measured |
|---|---|
| OS | Linux 7.0.0-14-generic, Ubuntu 26.04 |
| vCPU | 8 (`nproc`) |
| MemTotal | 15850224 kB ≈ **15.12 GiB** |
| Disk | `vda` 50 GiB, `lsblk` `ROTA=1` (virtio; not claimed as local SSD) |
| Docker | 29.1.3 + Compose 2.40.3 |
| Python for scripts | uv 0.11.26, CPython 3.12.13 |

`scripts/benchmark_m1.py --shape-only` rounded RAM to 15 GiB and set
`shape_ok: false` (exit 2). The script requires rounded GiB ≥ 16. This
host is marketed as 16 GB; the kernel reports 15.12. The threshold was
not edited.

## 3. What ran

Generated-secret Compose: `scripts/generate_local_secrets.py --env-file .env`
then `docker compose --env-file .env -f deploy/compose/compose.yaml up -d --build --wait`.
Sandbox image `tiny-hermes-sandbox:ci` was built on the host first and
its digest written into `.env`. `DETERMINISTIC_MODEL_DELAY_MS=50` for
the workspace drill; 3000 for the restart drill. All app services
became healthy, including `web`.

`scripts/benchmark_m1.py` (API `/health/ready` was 200) still printed
every §24.1 gate as `not_run` / `passed: false` with
`live driver for this gate is not executed in this invocation`, then
exited 2 because of the shape. The live drivers were never written.
A green shape would still be exit 1.

### 3.1 Workspace drill (main)

`uv run --no-sync python scripts/workspace_drill.py` **PASS**.

```
1MiB commits           runs=12  p50=0.62s  p95=1.59s
large commit           files=501  bytes=~100MiB  took=9.3s  worker_rss=100MiB
next run               status=completed  took=2.2s
leftovers              containers=0  volumes=0
```

These are whole-Run times through the public API, not the isolated
checkpoint cells in §24.1. The drill's own envelopes (10 s / 60 s / 15 s)
held. Against the product table as written: 1 MiB P95 1.59 s is above
the 1 s cell; the ~100 MiB commit 9.3 s is under 15 s; next-run 2.2 s
is under 3 s. That comparison is informational. It is not a §24.1 pass.

Worker crash recovery in the same drill: the first run reported
`recovered_in=30.0s` to `completed` (the drill waits for a terminal
status, not `queued`).

### 3.2 Restart drill

`DETERMINISTIC_MODEL_DELAY_MS=3000 uv run --no-sync python scripts/restart_drill.py`
**PASS**, 149.1 s. All four scenarios held. Event sequences stayed
contiguous. Sandbox leftovers 0.

Worker killed holding a lease: `lease expired status=queued seconds=27.38`,
then the worker returned and the run completed in 3.24 s. The product
cell is “可安全重试的 Run 在 30 秒内重新进入 `queued`”. 27.38 s is
under that cell for this one sample. It is still not the official
`worker_recovery` gate in `benchmark_m1.py`.

## 4. Official `benchmark_m1.py` document (raw)

`shape_ok` is false. `passed` is false. Every named gate is `not_run`.

```json
{
  "git_sha": "1a5ff96314f2f65c29093f12184fc282f383c7df",
  "shape": {"os": "linux", "vcpu": 8, "ram_gib": 15.115951538085938},
  "reference_shape": {"os": "linux", "vcpu": 8, "ram_gib": 16},
  "shape_ok": false,
  "deterministic_delay_ms": 50,
  "preload_run_events": 100000,
  "passed": false
}
```

## 5. Explicitly not claimed

- Product §24.1 passed.
- `0.1 Technical Preview`.
- Local SSD (the block device reports `ROTA=1`).
- 100k preloaded RunEvents.
- Live create_run / run_event / SSE / sandbox_cold / sandbox_warm drivers.
- GitHub Actions `compose-e2e` green.
- A Feishu WebSocket session.

The Compose listeners were bound on the public interfaces of each
instance. Each stack was **stopped** after the drills. Destroy or
firewall the instances; rotate the logins that were used to reach them.

## 6. Second host (8 vCPU / ~30 GiB) — same day

A second operator Linux spot instance. Same Ubuntu 26.04, Docker 29.1.3,
Compose 2.40.3, uv 0.11.26, CPython 3.12.13. `vda` 50 GiB, `ROTA=1`.
MemTotal 31951588 kB ≈ **30.47 GiB**. `nproc` 8.

`--shape-only` set `shape_ok: true` (exit 0 for the shape check;
overall `passed` stays false because `--shape-only` emits no gates).
`scripts/benchmark_m1.py` against a healthy API exited **1**: every
§24.1 gate is still `not_run` / `live driver for this gate is not
executed in this invocation`. Git SHA on that tree:
`d244a86edd9f5076b5595c24f828c9346980c7ba` (this record's first
commits on top of 4D).

Generated-secret Compose came up healthy. Workspace drill **PASS**:

```
1MiB commits           runs=12  p50=0.57s  p95=0.61s
large commit           files=501  bytes=~100MiB  took=8.5s  worker_rss=102MiB
next run               status=completed  took=2.2s
leftovers              containers=0  volumes=0
```

Worker crash in that drill: `recovered_in=29.8s` to `completed`.

Restart drill **PASS**, 149.0 s. Lease expired to `queued` in 27.29 s.

Informational vs the product table (still not a §24.1 pass): 1 MiB
whole-Run P95 0.61 s is under 1 s; ~100 MiB 8.5 s is under 15 s;
next-run 2.2 s is under 3 s; queued-after-kill 27.29 s is under 30 s.

```json
{
  "git_sha": "d244a86edd9f5076b5595c24f828c9346980c7ba",
  "shape": {"os": "linux", "vcpu": 8, "ram_gib": 30.47140884399414},
  "reference_shape": {"os": "linux", "vcpu": 8, "ram_gib": 16},
  "shape_ok": true,
  "passed": false
}
```
