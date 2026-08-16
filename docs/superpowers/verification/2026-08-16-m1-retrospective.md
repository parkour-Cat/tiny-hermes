# Looking back at 0.1 before planning M2 — 2026-08-16

## 1. Scope

Product roadmap §7, the last gate: *before creating the M2 plan, review 0.1's
installation, failed Runs, sandbox startup, API usage, and developer
feedback.* This is that review. Every claim below was produced on the fresh
Ubuntu 26.04 host from `2026-08-16-fresh-host-install.md`, on `main` at
`7e6c996`.

Four of the five areas can be measured. The fifth, developer feedback, cannot
be manufactured — §6 says what is and is not available in its place.

## 2. Installation

Measured twice on the same host: once against the document as it stood, once
against the document after it was repaired, from bare metal.

- Before: a reader with `docs/development.md` alone stalls. Five prerequisites
  with no install instructions, PowerShell-only blocks on a page addressed to
  a Linux host, a `python --version` check that cannot run on Ubuntu, Node 24
  absent from the distribution, no statement of where the source comes from,
  and the sandbox image — without which a tool-bound Agent cannot run at all —
  documented 500 lines below the place it is needed.
- After: the whole documented sequence executes verbatim and exits 0. Nine
  services healthy, bootstrap accepted, and the file-and-command task
  round-trips.

**Verdict: fixed, and the fix is walked.** The lesson worth carrying into M2
is not the list of gaps, it is that nobody had ever run this document on the
platform it describes. A document that has not been executed is a draft.

## 3. Failed Runs

This is the one place where 0.1 is genuinely hard to live with, and it is
worth stating precisely because the platform is *not* missing the information
— it is withholding it.

A Run whose command exits non-zero ends `failed`. Its snapshot from
`GET /api/v1/runs/{id}` has 22 fields:

```
agent_version_id, available_actions, blocked_by_run_id, budget,
budget_root_run_id, checkpoint_effect_status, checkpoint_replay_safe,
checkpoint_usage_quality, created_at, finished_at, id, last_event_sequence,
pause_reason, queue, retry_of_run_id, session_id, session_sequence,
started_at, state_version, status, wait_deadline_at, wait_kind
```

**None of them says why it failed.** `pause_reason` is null — it is for
pauses. `checkpoint` is not exposed. The `run_failed` event carries
`{"executed_ms": 50}` and nothing else, so an SSE subscriber learns no more
than a poller.

Meanwhile the database has the answer the whole time. The Run's checkpoint
reads:

```json
{"kind": "model_call", "stop_reason": "failed", "tokens": 32,
 "usage_quality": "provider", "failure": "deterministic_command_failed"}
```

So a developer whose Run failed can: read the Session transcript and infer
from a tool result's `exit_code`, or open Postgres. Neither is an API.

Two smaller observations from the same probe:

- A tool result records `exit_code: 3` with `failed: false`. That is correct —
  the command ran, the platform did not fail — but the pair reads oddly
  without the sentence explaining it.
- `available_actions: ["retry"]` is right there and useful. The verdict
  machinery is good; only the reason is missing.

**This is the highest-value thing M2 could fix, and it is small:** put the
checkpoint's `failure` on the Run snapshot and in the `run_failed` payload.

## 4. Sandbox startup

From the §24.1 run on the 16 GB host (`2026-08-16-m1-lease-ownership-fix.md`):

| Cell | p50 | p95 | Gate |
|---|---:|---:|---:|
| cold start, image cached | 476 ms | 514 ms | 3000 ms |
| warm reacquire | 21.2 ms | 29.8 ms | 300 ms |

Both are far inside their gates, and the container id does not change on a
warm reacquire. Nothing here asks for M2 work.

The one sandbox-shaped scar of this milestone is not startup, it is the lease:
a 100 MiB checkpoint outliving one renewal interval was rejected as
`LeaseLost` and swallowed without a log. Fixed (#12), but the shape of that
bug — *an error path that returns silently* — is worth grepping for before M2
adds more of them.

## 5. API usage

Driven end to end with `curl` from a shell that had never seen this platform:
bootstrap, sign-in, workspaces, Agent, draft, publish, Session, Runs, service
accounts, API keys, Chat Completions.

What is good, and should not be traded away:

- Errors are RFC-7807 shaped, with `code`, `title`, `detail`, and a
  `request_id`. `idempotency_key_reused`, `idempotency_key_required`,
  `workspace_required` each name the problem and the fix.
- Chat Completions errors are OpenAI-shaped where an OpenAI client will look:
  `{"error": {"message": "No published agent uses that alias.", "code":
  "model_not_found"}}`.
- A revoked key is a clean 401 with `A valid API key is required.`
- The idempotency contract is enforced, not advisory.

What cost time:

- **A foreign `X-Workspace-Id` returns `[]`, not a refusal.** Scoping a list
  to nothing is defensible and leaks nothing, but a developer who mistypes a
  workspace id sees an empty catalogue and concludes their data is gone. A
  `403` on a workspace the caller is not a member of would cost nothing and
  save an afternoon.
- **A tool round leaves no `run_events` row.** Reading `run_events` for a
  successful tool call shows `run_created`, `run_lease_acquired`,
  `sandbox_cache_reset`, `run_completed` and no trace of the command. This
  cost a wrong conclusion during the fresh-host walk, recorded there. The
  transcript is the right place — the API just never says so.
- The published spec's shape had to be read out of the source. The draft
  `PUT` accepts `tools`, `delivery`, `limits`, `model_policy`; the document
  shows one example and no field reference.

## 6. Developer feedback

There is none, and it should not be invented. Nobody outside the people who
built 0.1 has used it.

What exists instead, and is worth exactly what it is: this milestone's first
non-author operator was an agent working from the documentation on three
rented hosts. That produced §2's gap list, §3's finding, and §5's friction —
which is the same *kind* of evidence a first user produces, from someone who
cannot file a bug report or lose patience and leave.

**M2 should not be planned as if §6 were satisfied.** The honest state is that
0.1 has never met a user.

## 7. What M2 should carry out of this

Ranked by evidence, not by appetite:

1. **A failed Run must say why, in the API.** §3. Small, and the information
   already exists.
2. **Refuse a foreign workspace instead of answering emptily.** §5.
3. **Audit for silently swallowed errors.** The lease bug and the
   `DockerUnavailable` crash were both "an exception path that returns or
   raises the wrong thing, without a log". Two in one week, both found by
   accident.
4. **Put a real user in front of it before designing M2 features.** §6.

## 8. What this review cannot decide

- Whether the error text reads well *to this product's users*. Everything in
  §3 and §5 is one operator's judgment.
- M2's scope and order. §7 is a list of what 0.1 taught, not a plan.
- Whether `0.1 Technical Preview` should be tagged now. Gates 1–4 of roadmap
  §7 are met; this document is gate 5's material, and gate 5 asks for a review
  *before the M2 plan*, which is a decision, not a measurement.
