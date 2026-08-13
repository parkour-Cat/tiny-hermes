# M1 phase 4D acceptance verification — 2026-08-13

## 1. Scope

This record covers slice 4D of product closure, as specified by
`docs/superpowers/specs/2026-08-13-m1-product-closure-design.md` §10 and planned
in `docs/superpowers/plans/2026-08-13-m1-acceptance.md`: the thirteen §27.1
scenarios mapped to evidence, a benchmark whose thresholds are the product
§24.1 table, generated local Compose secrets, upgrade-from-previous-migration
CI, an operator path from bootstrap to Completions, and a Feishu laboratory
note. There is no Feishu adapter. Label-enumerated volume GC is a leftover,
not a 0.1 blocker.

0.1 Technical Preview is a gate **after** this phase. This slice does not
claim it.

The implementation commits, on `cursor/phase-4d-acceptance-c232` from the 4C
HEAD `96015bf` (`cursor/phase-4c-secrets-c232`), not from `main`:

| Commit | Result |
|---|---|
| `9cb55c5` | Plan M1 phase 4D acceptance, deploy, Feishu record. |
| `825e44e` | Generate local Compose secrets instead of the zeroes. |
| `16c6f36` | CI round-trips 4A (`0009`) and 4C (`0010`) migrations. |
| `a675452` | Operator path from bootstrap to Completions. |
| `9e83676` | Benchmark that cannot pass by editing the gate. |
| `8769a66` | Round marketed RAM so a 16 GB host is not rejected. |
| `0f3a99e` | Feishu long-connection facts without an adapter. |
| `887456f` | Map the thirteen M1 scenarios to evidence. |
| this record | Public description, record pins, this verification. |

## 2. Environment

Verification ran in the Cloud Agent workspace (Linux 6.12, Python 3.12, uv).
`nproc` is **4**. `MemTotal` is about **16 GiB**. PostgreSQL / Redis / MinIO
may be present; the API, Web, and Worker Compose services were **not**
running. Tokens, cookies, CSRF values, passwords, Secret plaintext, and KEK
material are absent from this record.

The Linux reference shape in product §24.1 is **8 vCPU**, 16 GB, local SSD.
This host does not match. The benchmark must not report a pass here.

## 3. What was verified by execution

**Generated secrets.** `uv run --no-sync pytest
packages/backend/tests/unit/scripts/test_generate_local_secrets.py` **5
passed**, including `docker compose … config` interpolating a minted KEK onto
the API service and **not** onto the Controller.

**CI pin.** `test_ci_migrations.py` asserts the workflow downgrades
`20260813_0010` then `20260813_0009` before the existing 0008-to-base chain.
A live Alembic round-trip against `tiny_hermes_test` was not executed here
(`pg_isready` did not see the test database from this shell).

**Benchmark unit.** `test_benchmark_m1.py` **11 passed**. Every §24.1 cell is
a frozen constant. A 4 vCPU shape is rejected. `--shape-only` exits 2.
`evaluate` fails when P95 or error rate exceeds the gate. There is no
`allow_skip` / `relax` path. Default `main` with a mocked-ready shape and a
down API exits nonzero.

On this host, `python scripts/benchmark_m1.py --shape-only` exited **2** with
`vcpu: 4`, `ram_gib ≈ 15.64`, `shape_ok: false`, `passed: false`, git SHA of
the benchmark commit. Live gates were not run. 3C's workspace-drill numbers
are not this benchmark.

**Acceptance records.** `test_acceptance_records.py` asserts the Feishu note
has measured / unconfirmed / Webhook-fallback columns, the closure record has
rows 1–13, and `pyproject.toml` description is `单 Agent 安全运行骨架`.

**Regression.** Backend unit suite **639 passed** (4C's 618 plus 4D script
and record pins). Focused 4C integration and web vitest were not re-run in
this record.

## 4. Explicitly not claimed

- §24.1 thresholds passing on this host (4 vCPU; API/Web/Worker down).
- A live generated-secret Compose `up --wait` of API/Web/Worker.
- Playwright, restart_drill, workspace_drill re-runs.
- A Feishu WebSocket session or disconnect-gap measurement.
- Label-enumerated volume GC (engine already lists by label; Scheduler would
  need a new Controller socket action; an orphan still requires a crash in
  teardown's last step).
- `0.1 Technical Preview` as a release label.

## 5. Exit criteria

| Criterion | Evidence |
|---|---|
| §27.1 scenarios 1–13 have automated or dated evidence | `2026-08-13-m1-product-closure.md` |
| Benchmark pins §24.1; shape mismatch cannot report pass | `scripts/benchmark_m1.py` + unit tests |
| Generated secrets interpolate into Compose; Controller has no KEK | generate-local-secrets tests + 4C compose boundary |
| CI round-trips 0010 and 0009 | `ci.yml` + `test_ci_migrations.py` |
| Feishu note has the three columns | `2026-08-13-m1-feishu.md` |
| Public description is “单 Agent 安全运行骨架” | `pyproject.toml` |
