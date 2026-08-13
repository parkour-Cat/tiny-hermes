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
| `308103e` | Record what 4D acceptance could and could not prove. |
| `8e228bb` | Silent `shell.exec` keep-alive, merged up into 4D. |
| `fbeab72` | Bind tools via the Ant Design checkbox wrapper. |
| `fe9c5ba` | Wait for any assistant turn when a bound tool replies twice. |
| this record | Public description, record pins, later local Compose run. |

## 2. Environment

Verification ran in the Cloud Agent workspace (Linux 6.12, Python 3.12, uv).
`nproc` is **4**. `MemTotal` is about **16 GiB**. Tokens, cookies, CSRF
values, passwords, Secret plaintext, and KEK material are absent from this
record.

The first 4D pass did not start API / Web / Worker. A later pass on the
**same host** brought `deploy/compose/compose.yaml` up (sandbox image
`tiny-hermes-sandbox:ci`) and ran Playwright plus the three drills. This
host still is not the §24.1 Linux reference shape (8 vCPU, 16 GB, local
SSD). The benchmark must not report a pass here.

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

**Regression.** Backend unit suite **641 passed** on a later local run
(ruff + pyright 0 errors). Integration **397 passed** against
`tiny_hermes_test` on `127.0.0.1`. Web vitest AgentDetailPage **17 passed**
after the unbound-tools pin.

### 3.1 Later local Compose run (same 4 vCPU host)

Docker socket and credential boundaries in the running stack: Controller has
the socket and no MinIO / model key; API and Worker do not see the socket;
Worker has MinIO credentials.

`pnpm exec playwright test --config tests/e2e/playwright.config.ts`
**7 passed** (26.4s) after two walk fixes: Ant Design 6's opacity-0 input
does not toggle under `locator.check()`, so the builder clicks the visible
wrapper; `continue_once` with `file.list` bound yields two assistant role
tags, so the walk asserts an assistant turn appeared rather than uniqueness.

`scripts/restart_drill.py` with `DETERMINISTIC_MODEL_DELAY_MS=3000`: all
four scenarios held, **147.6s**.

`scripts/workspace_drill.py` (main): PASS. 1 MiB commits n=12 p50=6.93s
p95=6.95s (drill envelope 10s, not §24.1's 1s checkpoint cell). Silent
~100 MiB commit 12.4s, worker RSS 145 MiB. Leftovers 0.

`scripts/workspace_drill.py --phase quota` with `WORKSPACE_MAX_BYTES=8388608`
on a clean stack (`DETERMINISTIC_MODEL_DELAY_MS` default 50): PASS.
over-quota paused `reason=limit` in 0.8s; resume completed; old revision
kept. Leftovers 0.

This host's leftover `iptables-legacy` FORWARD DROP blocked compose-bridge
ICC until `DOCKER-USER` accepted the project bridge. That is this VM's
firewall state, not a product defect. GitHub-hosted runners are not this
machine.

## 4. Explicitly not claimed

- §24.1 thresholds passing on this host (4 vCPU). Live `benchmark_m1.py`
  gates were not run; `--shape-only` still exits 2.
- GitHub Actions `compose-e2e` green. Private-repo spending limit refused
  the runner after the silent-exec fix was pushed.
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
