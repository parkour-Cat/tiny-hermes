# M1 Phase 4D Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or
> superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Deliver slice 4D of product closure: the thirteen §27.1 scenarios
each have automated or dated evidence, a `scripts/benchmark_m1.py` whose
thresholds are the product §24.1 table (never edited to make a run green),
generated-secret Compose, upgrade-from-previous-migration CI, an operator
path that creates an Agent / Playground / Completions key, and a Feishu
laboratory note with measured / unconfirmed / Webhook-fallback columns.
No Feishu adapter code. Label-enumerated volume GC is not a 0.1 blocker.

**Architecture:** Evidence first. Scenarios already proven in 2A–4C are
linked and re-run as regression, not re-litigated. The benchmark script is
a gate against a live Linux reference stack (8 vCPU, 16 GB, local SSD, 50 ms
deterministic delay, image cached, 100k preloaded RunEvents). This Cloud
Agent host is 4 vCPU / 16 GB and does not run API/Web/Worker Compose, so
the script must refuse to claim a §24.1 pass here.

**Tech Stack:** existing Compose, Alembic, pytest, httpx, the restart-drill
console helpers.

**Authority:** `docs/superpowers/specs/2026-08-13-m1-product-closure-design.md`
§10. Product design v2.4 §§24.1 and 27.1 win ties. 4C verification:
`docs/superpowers/verification/2026-08-13-m1-secrets.md`.

---

## 1. Working rules

- Branch from the 4C HEAD (`cursor/phase-4c-secrets-c232`), not from `main`.
- Observe a focused failing test before production code. Commit when it passes.
- Database integration tests target only `tiny_hermes_test` at `127.0.0.1`.
- Use `uv run --no-sync`; do not run `ruff format`.
- Do not lower a §24.1 number because a run failed. Record the raw result.
- Do not pretend Compose API/Web/Worker or a Feishu WebSocket were executed
  in this environment.
- After every task, append a short note to
  `.superpowers/m1-implementation-learning-notes.md` (gitignored).

## 2. File map (expected)

```text
docs/superpowers/plans/2026-08-13-m1-acceptance.md
docs/superpowers/verification/2026-08-13-m1-product-closure.md
docs/superpowers/verification/2026-08-13-m1-feishu.md
docs/superpowers/verification/2026-08-13-m1-acceptance.md
scripts/generate_local_secrets.py
scripts/benchmark_m1.py
packages/backend/tests/unit/scripts/test_generate_local_secrets.py
packages/backend/tests/unit/scripts/test_benchmark_m1.py
.github/workflows/ci.yml          # downgrade 0010 / 0009
docs/development.md               # operator walkthrough
pyproject.toml                    # public description
```

Volume GC stays a leftover: the engine already lists by label, but the
Scheduler would need a new Controller socket action. An orphan still
requires a crash in teardown's last step.

---

### Task 1: Plan

- [ ] **Step 1: Write this plan**
- [ ] **Step 2: Commit** `docs: plan M1 phase 4D acceptance, deploy, Feishu record`

### Task 2: Generated local secrets

**Files:** `scripts/generate_local_secrets.py` + unit test.

Emits `SESSION_COOKIE_SECRET`, `BOOTSTRAP_TOKEN` (≥32 random characters) and
`TINY_HERMES_KEK` (32-byte standard base64). Refuses to overwrite `.env`
without `--force`. `docker compose … config` with the generated file must
interpolate those values onto `x-app-env` (not onto the Controller).

- [ ] **Step 1: Failing tests** (length, KEK decode, no overwrite, compose config)
- [ ] **Step 2: Implement**
- [ ] **Step 3: Commit** `feat: generate local compose secrets instead of the zeroes`

### Task 3: Upgrade-from-previous-migration CI

**Files:** `.github/workflows/ci.yml`

Head is `20260813_0011`. After `alembic check`, add downgrade/upgrade for
`20260813_0010` and `20260813_0009` before the existing 0008…base chain.

- [ ] **Step 1: Add the two steps**
- [ ] **Step 2: Commit** `ci: round-trip the 4A and 4C migrations`

### Task 4: Operator path

**Files:** `docs/development.md`

A short walkthrough that actually: generates secrets, starts Compose, creates
two workspaces, creates and publishes an Agent from the console, sends from
Playground, mints an API Key, and calls Chat Completions. Reuse existing
PowerShell snippets; do not invent a second protocol.

- [ ] **Step 1: Write the walkthrough**
- [ ] **Step 2: Commit** `docs: an operator path from bootstrap to completions`

### Task 5: Benchmark script

**Files:** `scripts/benchmark_m1.py` + `packages/backend/tests/unit/scripts/test_benchmark_m1.py`

Pin every §24.1 cell as a named constant. Percentile arithmetic. Host shape
(Linux, vCPU, RAM). Git SHA. JSON report fields: P50/P95/P99, error rate,
CPU, RSS, sha. `--shape-only` exits nonzero when the host is not 8 vCPU /
≥16 GiB. Live gates need Compose; they print raw numbers and fail closed.
Never include a path that lowers a threshold.

- [ ] **Step 1: Failing tests** (thresholds, percentiles, shape refusal, report shape)
- [ ] **Step 2: Implement the script**
- [ ] **Step 3: Commit** `feat: a benchmark that cannot pass by editing the gate`

### Task 6: Feishu laboratory note

**Files:** `docs/superpowers/verification/2026-08-13-m1-feishu.md`

No adapter. Columns: measured / unconfirmed / Webhook-fallback decision.
Sources: official long-connection docs. This environment did not open a
Feishu WebSocket.

- [ ] **Step 1: Write the note from official docs, mark what was not measured**
- [ ] **Step 2: Commit** `docs: record Feishu long-connection facts without an adapter`

### Task 7: §27.1 closure record

**Files:** `docs/superpowers/verification/2026-08-13-m1-product-closure.md`

Thirteen rows. Link 2A–4C verification and the tests that still run. Scenario
11 is the Feishu note. Scenario 13 is the benchmark script: dated, not
claimed green on this host.

- [ ] **Step 1: Write the thirteen-row record**
- [ ] **Step 2: Commit** `docs: map the thirteen M1 scenarios to evidence`

### Task 8: 4D verification and public description

**Files:** `docs/superpowers/verification/2026-08-13-m1-acceptance.md`,
`pyproject.toml` description, `docs/development.md` if needed.

Public description remains “单 Agent 安全运行骨架”. Do not claim 0.1
Technical Preview. Volume GC leftover named.

- [ ] **Step 1: Run checks; write what they produced**
- [ ] **Step 2: Commit** `docs: record what 4D acceptance could and could not prove`

## 3. Explicit non-goals

- Feishu adapter, Web Chat, Approvals, Usage, task tree.
- Claiming §24.1 pass on a 4 vCPU Cloud Agent host.
- Lowering any §24.1 threshold.
- Label-enumerated volume GC (leftover, not a 0.1 blocker).
- Marking the repository `0.1 Technical Preview` — that gate is after this
  phase, once a Linux reference host actually runs the benchmark.

## 4. Exit criteria

- [ ] §27.1 scenarios 1–13 have automated or dated evidence.
- [ ] `scripts/benchmark_m1.py` pins §24.1; shape mismatch cannot report pass.
- [ ] Generated secrets interpolate into Compose; Controller still has no KEK.
- [ ] CI round-trips 0010 and 0009.
- [ ] Feishu note has measured / unconfirmed / Webhook-fallback columns.
- [ ] Public description is “单 Agent 安全运行骨架”.
