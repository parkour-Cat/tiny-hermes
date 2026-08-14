# M1 product-closure §27.1 evidence — 2026-08-13

Each of product design v2.4 §27.1's thirteen scenarios is either an automated
check that still lives in the tree, or a dated record. Scenarios already
proven in phases 2A–4C are **linked**, not re-litigated. The first 4D pass
did not start API/Web/Worker. A later pass on the same 4 vCPU Cloud Agent
host ran Playwright and the three drills; that is not a §24.1 pass.

Slice design: `docs/superpowers/specs/2026-08-13-m1-product-closure-design.md`
§10.1. 4D plan: `docs/superpowers/plans/2026-08-13-m1-acceptance.md`.

| # | Scenario | Evidence | Kind |
|---|---|---|---|
| 1 | Start the platform, bootstrap a local account, create two workspaces | `docs/superpowers/verification/2026-08-10-m1-foundation.md`; `tests/e2e`; `tests/integration/tenancy/test_workspace_api.py`; operator walkthrough in `docs/development.md` | automated + dated. Later 4D Compose run: Playwright foundation walk passed. |
| 2 | Create an Agent draft, test in Playground, publish an immutable version | `docs/superpowers/verification/2026-08-13-m1-console.md`; Vitest AgentDetailPage / PlaygroundPage; Playwright walks in `tests/e2e/console.spec.ts` | automated. Later 4D Compose run: Playwright **7 passed**, including bind `file.list`, Playground send, rollback to v1. |
| 3 | Runs API + Chat Completions + SSE; two default Completions → two ephemeral Sessions; `X-Tiny-Hermes-Session-Id` reuses persistent; blocked persistent → 409 and **zero** new Runs | `docs/superpowers/verification/2026-08-13-m1-machine-identity.md`; `tests/integration/runs/test_chat_completions.py` | automated |
| 4 | Repeat the same Idempotency-Key → one Run | `docs/superpowers/verification/2026-08-10-m1-run-foundation.md`; `tests/integration/runs/test_run_creation.py` | automated |
| 5 | Three messages in one Session: only head is claimed, FIFO after; cancel the second pending, finish the first, third becomes head; blocked send returns `session_blocked` with reason, position, actions; bad `head_run_id` → scheduler `session_head_repaired` | `docs/superpowers/verification/2026-08-10-m1-run-execution.md`; `tests/integration/runs/test_run_coordination.py`, `test_scheduler.py`; 4A queue snapshot | automated |
| 6 | Pause, resume, cancel; illegal transitions refused | run-execution verification; `tests/integration/runs/test_run_control.py` | automated |
| 7 | Forced Docker sandbox: multi-tool slice without rebuild, idle reacquire keeps container id, `/workspace/data` restore, `cache_state=reset`, next Run new writable layer, no host fallback | `docs/superpowers/verification/2026-08-11-m1-sandbox.md`; `docs/superpowers/verification/2026-08-11-m1-session-workspace.md`; `scripts/workspace_drill.py` | automated + dated drill. Later 4D Compose run: workspace drill PASS (1 MiB p95=6.95s, ~100 MiB 12.4s); quota drill PASS on a clean stack. |
| 8 | Restart API, Worker, Scheduler; sessions, audit, events, retry-safe Runs recover; expired leases not double-reclaimed; concurrent event sequences stay unique | `scripts/restart_drill.py`; run-execution verification; CI restart-drill job | automated + dated drill. Later 4D Compose run: restart drill PASS, 147.6s. |
| 9 | Another workspace identity requesting a known id is refused without payload; missing `X-Workspace-Id` is a clear error; an API Key cannot switch workspace with that header | foundation verification; 4A `tests/integration/runs/test_runs_api_key.py` | automated |
| 10 | Model endpoint at loopback / metadata / unapproved private / redirect to those: `SafeOutboundClient` refuses. A raw HTTP client outside the outbound package fails the architecture check | `docs/superpowers/verification/2026-08-11-m1-real-model.md`; `tests/unit/outbound`; ruff `banned-api` in `pyproject.toml` | automated |
| 11 | Feishu long-connection laboratory note with measured / unconfirmed / Webhook-fallback columns | `docs/superpowers/verification/2026-08-13-m1-feishu.md` | dated note. No adapter. Disconnect-gap **unconfirmed**. |
| 12 | Derived retries share `budget_root_run_id`, consume remaining budget, default max 3 successes; Run Detail shows origin and offers or disables retry | run-execution `tests/integration/runs/test_run_retry.py`; 4B Run Detail Vitest | automated |
| 13 | Run §24.1 on the Linux reference shape (8 vCPU, 16 GiB, 50 ms delay, image cached, 100k RunEvents) | `scripts/benchmark_m1.py`; unit pins in `packages/backend/tests/unit/scripts/test_benchmark_m1.py`; later host: `2026-08-14-m1-reference-host.md` | **script exists; not a pass.** Cloud Agent is 4 vCPU. The 2026-08-14 operator host is 8 vCPU / ≈15.12 GiB; `--shape-only` exits 2 and every live gate is `not_run`. Workspace and restart drills passed there. Do not edit thresholds to make a run green. |

## Same-Run agreement (Runs API, SSE, console, Completions)

4A verification: Completions 409 inserts zero Runs; the queue snapshot names
the head and the head's `available_actions`. 4B: Playground is a Runs API
client with a cookie; Run Detail reads the same transcript/tools/files.
Those contracts still have tests. A single live Run observed from all four
surfaces was not captured in this environment.

## Secrets (supporting, not a §27.1 row)

4C verification: create returns a mask; listing has no plaintext; wrong KEK
leaves the row unchanged; rewrap resumes. Local Compose still uses the env
`credential_ref` overlap until an operator points an endpoint at a Secret
id.

## Public description

`pyproject.toml` `description` is `单 Agent 安全运行骨架`. This phase does
**not** mark `0.1 Technical Preview`; that gate is after a Linux reference
host actually runs the benchmark.
