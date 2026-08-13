# M1 phase 4A machine identity verification — 2026-08-13

## 1. Scope

This record covers slice 4A of product closure, as specified by
`docs/superpowers/specs/2026-08-13-m1-product-closure-design.md` and planned in
`docs/superpowers/plans/2026-08-13-m1-machine-identity.md`: the blocked-session
snapshot Chat Completions and Playground both need, ServiceAccount + API Key
auth, inbound `POST /v1/chat/completions`, the sync-lease exception, and the 3C
artifact recorder wired through `shell.exec`.

4B (console / Playground), 4C (Secrets / KEK), and 4D (benchmarks, Feishu,
§24.1) are not in this slice.

The implementation commits, on `cursor/phase-4a-machine-identity-c232` from
`main` at `5b28b96`:

| Commit | Result |
|---|---|
| `47b5377` | Design M1 phase 4 and the 4A machine-identity plan. |
| `193b1d4` | A blocked Run snapshot names the head and what the caller can do to it. |
| `a8a583b` | Mint an API Key that authenticates as a ServiceAccount. |
| `20cd20a` | An Agent may opt into Chat Completions without rewriting old versions. |
| `bc6334b` | Chat Completions creates an ephemeral Session unless a header says otherwise. |
| `c09d431` | A Completions Run holds its lease until it finishes or the minute is up. |
| `c160fcf` | Completions can stream tokens without turning them into RunEvents. |
| `d001459` | Restore the `openai_model` module docstring (Task 9 had closed it early). |
| `be07d80` | Long `shell.exec` output is an Artifact, not a truncated shrug. |

## 2. Environment

Verification ran in the Cloud Agent workspace (Linux 6.12, Python 3.12, uv,
PostgreSQL 17 on `127.0.0.1:5432`, MinIO `:9000`). Database commands targeted
only `tiny_hermes_test`. Tokens, cookies, CSRF values, passwords, Run input, and
Agent personality text are absent from this record.

## 3. What was verified by execution

**Static checks.** `uv run --no-sync ruff check packages/backend` passed.
`uv run --no-sync pyright` reported 0 errors. `uv run --no-sync alembic check`
reported no new upgrade operations. Head is `20260813_0010` (`delivery_mode` on
`runs`, after `20260813_0009` machine-identity tables).

**Unit, 572 tests.** Includes the pinned unbound / deterministic content hash
`4fcf412e8da2d827a601dbc4390d072a21bef593c080a2076041edb4ffb6deaf` (unchanged by
default `AgentSpec.delivery`), Completions slice policy (`hold_slice` vs
`compat_timeout`), and `shell.exec` preview / `artifact_id` /
`artifact_store_failed` formatting with no store key in the tool result.

**Integration against real PostgreSQL and MinIO (focused 4A evidence, 70
passed in one run; artifact wiring 12 more across artifact files; worker
tools + workspace 95 including the new stream path):**

- Runs API `session_blocked` snapshot carries `head_status`, `head_reason`,
  and the **head's** `available_actions`.
- API Key plaintext is returned once; listing has `prefix` and never `token`.
- Bearer caller on Sessions/Runs is the ServiceAccount. Key ∩ role is
  enforced; a disagreeing `X-Workspace-Id` is generic 403.
- Completions is Bearer-only (`POST /v1/chat/completions`, no `/api` prefix).
  Cookie POST is 401. Default Session is ephemeral; `X-Tiny-Hermes-Session-Id`
  must name a persistent Session owned by this caller and Agent.
- Completions 409 `session_blocked` inserts **zero** new `runs` rows
  (`test_a_blocked_persistent_session_creates_zero_runs` compared
  `SELECT count(*) FROM runs` before and after).
- Sync lease: `delivery_mode=chat_completions` does not `SLICE_ENDED` inside
  `sync_timeout_seconds`; window expiry is `paused(compat_timeout)`; Scheduler
  cancels that pause after 24h.
- Streamed Completions: admit before SSE headers; later pause is `event: error`
  without `[DONE]`. Fingerprint includes `stream`. Tokens are not RunEvents.
- Long `shell.exec` output: tool result contains `artifact_id=` and
  `GET /api/v1/artifacts/{id}/content` returns the bytes. Short output opens
  no `artifacts` row.

### 3.1 Snapshot example (Runs API, blocked Session)

A pending Run created against a `paused(manual)` head returns 201 with:

```json
{
  "queue": {
    "status": "session_blocked",
    "blocked_by_run_id": "<head-run-id>",
    "position": 2,
    "head_status": "paused",
    "head_reason": {
      "pause_reason": "manual",
      "wait_kind": null,
      "wait_deadline_at": null
    },
    "available_actions": ["resume", "cancel"]
  }
}
```

Top-level `blocked_by_run_id` remains. The head's own snapshot omits
`head_status` / `head_reason`. Completions against the same Session returns
409 with the same facts plus `session_id` and `runs_api_url`, and does not
insert a Run.

### 3.2 Routes added or widened in 4A

| Method | Path | Auth |
|---|---|---|
| POST | `/api/v1/service-accounts` | cookie + CSRF, workspace admin |
| GET | `/api/v1/service-accounts` | cookie |
| POST | `/api/v1/service-accounts/{id}/disable` | cookie + CSRF |
| POST | `/api/v1/service-accounts/{id}/api-keys` | cookie + CSRF; `token` once |
| GET | `/api/v1/service-accounts/{id}/api-keys` | cookie; never `token` |
| POST | `/api/v1/api-keys/{id}/revoke` | cookie + CSRF |
| POST | `/v1/chat/completions` | Bearer only |

Sessions, Runs, SSE, and Artifacts accept Bearer or cookie (dual-auth).
Completions does not accept a session cookie as a substitute.

## 4. What is asserted rather than executed locally

GitHub Actions for this branch was still running when this record was written
(`31704537245` / `31704534008` on pull request 3). Compose e2e, Playwright, and
the workspace drill are CI's to prove, as they were in 3C. The full artifact
download through a live Docker `shell.exec` (not the Worker's stream fake) can
wait for 4B's Playground scenario; the Worker wiring and MinIO round-trip are
what 4A required.

## 5. Standing leftover (not 4A)

- **Playground, Agent Builder tools checklist, Run Detail files UI, i18n** —
  slice 4B. The console still has the phase-2C / 3A shell.
- **Secrets and KEK rewrap** — slice 4C. Endpoint credentials remain
  environment variable names.
- **Product design §24.1 benchmarks, Feishu technical note, volume-orphan
  label sweep** — slice 4D.
- Completions OpenAI errors stay in the OpenAI envelope; auth 401 may still
  be Problem Details.
- `paused(compat_timeout)` ages out at 24h via the Scheduler; there is no
  resume of a timed-out Completions Run through Completions itself
  (`requires_runs_api` for other pauses).

## 6. Slice 4A completion checklist

- [x] Blocked Runs API snapshot matches technical design §13.2.
- [x] API Key plaintext is returned once; listing never includes it.
- [x] Caller on Sessions/Runs/idempotency is the ServiceAccount, not the key.
- [x] Key ∩ role is enforced; header cannot switch workspace.
- [x] Completions default Session is ephemeral; explicit header is persistent.
- [x] Completions 409 `session_blocked` inserts no Run.
- [x] Sync Run does not requeue on an ordinary slice boundary inside the window.
- [x] `paused(compat_timeout)` exists and ages out.
- [x] Streamed errors after headers are an SSE `error` event.
- [x] Long `shell.exec` output is a tenant-scoped Artifact.
- [x] Published Agent content hashes from phases 2–3C are unchanged.
