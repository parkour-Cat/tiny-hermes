# M1 Phase 4A Machine Identity and Chat Completions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or
> superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Deliver slice 4A of product closure: the blocked-session snapshot Chat
Completions and Playground both need, ServiceAccount + API Key auth, inbound
`POST /v1/chat/completions`, the sync-lease exception, and the 3C artifact
recorder wired through `shell.exec`.

**Architecture:** Browser cookie auth stays. A parallel Bearer path authenticates
a hashed API Key to a workspace-scoped ServiceAccount; `CallerIdentity` already
reserves `service_account`. Chat Completions is a thin adapter over existing
RunCoordination, with two deliberate incompatibilities vs Runs API: 409 and no
Run when a persistent Session is blocked; a sync WorkerLease that does not
`SLICE_ENDED` inside `sync_timeout_seconds`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL 17,
pytest, existing Runs/SSE/Worker.

**Authority:** `docs/superpowers/specs/2026-08-13-m1-product-closure-design.md`.
Product design v2.4 §18.1–18.3 and technical design v1.1 §§12–13.3 win ties.

---

## 1. Working rules

- Branch from current `main`. Do not reopen `m1-sandbox`.
- Observe a focused failing test before production code. Commit when it passes.
- Database integration tests target only `tiny_hermes_test`.
- Use `uv run --no-sync`; do not run `ruff format`.
- Cookie routes keep CSRF. Bearer routes must not require CSRF and must not
  accept a session cookie as a substitute on `/v1/chat/completions`.
- Default `AgentSpec` bytes and content hashes of already-published versions
  must not change.
- After every task, append a short note to
  `.superpowers/m1-implementation-learning-notes.md` (gitignored).

## 2. File map (expected)

```text
packages/backend/src/tiny_hermes/identity/     # ServiceAccount + API Key live here,
                                               # next to User auth — same bounded context
packages/backend/src/tiny_hermes/runs/         # snapshot queue shape; delivery_mode;
                                               # Completions adapter; sync slice policy
packages/backend/src/tiny_hermes/artifacts/    # no new types; Worker calls the recorder
packages/backend/src/tiny_hermes/agents/       # AgentSpec.delivery widening
migrations/versions/20260813_0009_machine_identity.py
```

Exact filenames are chosen at the task, not here. New tables do not go in the
runs module.

---

### Task 1: Queue snapshot names the blocking head

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/domain/models.py`
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py`
- Modify: `apps/web/src/api/types.ts` (type only; UI still 4B)
- Test: `packages/backend/tests/integration/runs/test_run_creation.py`
- Test: `packages/backend/tests/unit/runs/` (snapshot document)

- [ ] **Step 1: Write the failing snapshot test**

A Session whose head is `paused(manual)` accepts a second Run. Assert 201, a
row exists, and:

```python
queue = body["queue"]
assert queue["status"] == "session_blocked"
assert queue["blocked_by_run_id"] == head_id
assert queue["position"] == 2
assert queue["head_status"] == "paused"
assert queue["head_reason"] == {
    "pause_reason": "manual",
    "wait_kind": None,
    "wait_deadline_at": None,
}
assert set(queue["available_actions"]) == {"resume", "cancel"}
assert "pause" in body["available_actions"] or "cancel" in body["available_actions"]
```

Top-level `blocked_by_run_id` remains. Head/pending/terminal snapshots omit
`head_status` / `head_reason`.

- [ ] **Step 2: Run and fail on the short `queue` object**

- [ ] **Step 3: Widen `RunSnapshot.document()` and the assembler**

`queue.available_actions` is `RunStateMachine.available_actions` of the **head**
for this caller. Do not reuse the pending Run's list.

- [ ] **Step 4: Verify and commit**

`fix: a blocked run snapshot names the head and what the caller can do to it`

---

### Task 2: ServiceAccount and API Key schema

**Files:**
- Create: migration `20260813_0009_machine_identity.py`
- Create: tables, domain types, SQL store in `identity/`
- Test: `packages/backend/tests/integration/identity/test_machine_identity_migration.py`

- [ ] **Step 1: Write the failing migration test**

Tables `service_accounts` and `api_keys` with the columns in the design §7.1–7.2.
Check constraints: role in `{developer, viewer}`; scopes subset of the closed
set; `token_digest` unique; `prefix` indexed.

- [ ] **Step 2: Run and fail**

- [ ] **Step 3: Migration + ORM**

No plaintext column. `caller_type = service_account` already allowed on
sessions and idempotency records.

- [ ] **Step 4: up/down/up and commit**

`feat: give a workspace a service account that can hold a key`

---

### Task 3: Bearer authentication

**Files:**
- Modify: `identity/presentation/dependencies.py` (or a sibling)
- Modify: `runs/presentation/errors.py` `actor_of` / caller mapping
- Test: unit for digest compare, revoked, expired, disabled account, workspace
  header mismatch

- [ ] **Step 1: Failing tests for the verifier**

Prefix lookup → digest compare → reject revoked/expired/disabled. Header
`X-Workspace-Id` absent ⇒ workspace from the account. Present and different ⇒
403 without leaking whether the other workspace exists.

- [ ] **Step 2: Implement `authenticate_api_key` parallel to cookie auth**

Produces `Actor` plus `CallerIdentity(SERVICE_ACCOUNT, account_id)`. Role is
the account's role. Scopes hang on a request-scoped object the route checks.

- [ ] **Step 3: Commit**

`feat: a bearer token names a service account, never a workspace header`

---

### Task 4: ServiceAccount and API Key HTTP

**Files:**
- Create: identity (or tenancy-adjacent) routes
- Test: integration for create/list/disable/revoke; plaintext only on create

Suggested routes (collection under `/api/v1`, workspace header required for
browser):

```text
POST   /api/v1/service-accounts
GET    /api/v1/service-accounts
POST   /api/v1/service-accounts/{id}/disable
POST   /api/v1/service-accounts/{id}/api-keys
GET    /api/v1/service-accounts/{id}/api-keys
POST   /api/v1/api-keys/{id}/revoke
```

Exact paths may follow FastAPI conventions already used for agents; do not
invent a second style.

- [ ] **Step 1: Failing tests**

Workspace admin creates an account (developer) and a key with `runs.write`.
Create body contains the `thk_` token once. List does not. Viewer creating a
key with `runs.write` is 403 even if they somehow POST (viewer cannot create
keys). Developer listing keys: yes. Viewer listing: no.

Intersection: a developer account whose key has only `runs.read` cannot POST
`/api/v1/runs` (tested in Task 5).

Audit events on create/disable/revoke.

- [ ] **Step 2: Implement and commit**

`feat: mint an api key whose plaintext the listing cannot remember`

---

### Task 5: Runs API accepts a key

**Files:**
- Modify: session/run/artifact/event dependencies to take cookie **or** Bearer
- Test: integration — key with `runs.write` creates a Run; `caller_type` is
  `service_account`; idempotency is scoped to the account id; a second key on
  the same account replays; a key on another account with the same
  Idempotency-Key creates a different Run
- Test: `runs.read` cannot POST; `runs.control` required for pause
- Test: key cannot use `X-Workspace-Id` to reach another workspace (generic 403)

Chat Completions is Task 7. This task is the Runs/Sessions/SSE/Artifacts path
so Playground's future key-less cookie client and the machine client share one
coordination layer.

- [ ] **Step 1: Failing tests as above**

- [ ] **Step 2: Dual-auth dependency; CSRF only on cookie writes**

- [ ] **Step 3: Commit**

`feat: runs api accepts a key without forgetting csrf for browsers`

---

### Task 6: AgentSpec delivery block, hash-stable

**Files:**
- Modify: `agents/domain/models.py`
- Test: existing content-hash pin; new tests for enable + timeout bounds

- [ ] **Step 1: Failing test that a spec with only personality/model/limits/tools
  still hashes as before**

- [ ] **Step 2: Add `delivery` with defaults that dump to the same JSON as an
  omitted field** — if Pydantic includes default subobjects, stop that: omit
  when equal to the default, or accept that this **is** a hash break and bump
  nothing only if the pin test can be updated with a written reason. Prefer
  omit-default so `schema_version` stays 1 and old rows stay valid.

- [ ] **Step 3: Commit**

`feat: an agent may opt into chat completions without rewriting old versions`

---

### Task 7: Non-streaming Chat Completions

**Files:**
- Create: `runs/presentation/completions.py` (or `compat/`)
- Modify: `api/app.py` to mount `POST /v1/chat/completions`
- Test: integration with deterministic Agent

- [ ] **Step 1: Failing tests**

1. Cookie POST to `/v1/chat/completions` is 401/403 — not a CSRF bypass.
2. Key + published alias, `delivery.chat_completions.enabled=true`, no session
   header → 200, ephemeral Session, one completed Run, assistant text in
   `choices[0].message.content`.
3. Same key, `X-Tiny-Hermes-Session-Id` of a persistent Session this caller
   owns for this Agent → second request appends to the same Session.
4. Header naming another workspace's Session, a different Agent, or another
   caller → error, no Run, no fallback to ephemeral.
5. Persistent Session with paused head → 409, OpenAI envelope, `code` /
   extension fields from the design, **zero** new `runs` rows.
6. Agent with `enabled=false` → `agent_not_compatible`.
7. Unknown alias → `model_not_found`.
8. `Idempotency-Key` replays; different body → 409.

- [ ] **Step 2: Implement as an adapter over RunCoordination**

Wait for the Run to reach a terminal or pause/wait state, bounded by
`sync_timeout_seconds` (Task 8 installs the pause; this task may wait on
`completed`/`failed` only and leave timeout to Task 8 if that is smaller).
Prefer implementing the wait loop here with a short timeout in tests via the
delivery field.

- [ ] **Step 3: Commit**

`feat: chat completions creates an ephemeral session unless a header says otherwise`

---

### Task 8: Sync lease and `paused(compat_timeout)`

**Files:**
- Modify: `runs/domain/slice_policy.py`, Worker, Run row or checkpoint flag
- Modify: Scheduler wait/pause cleanup
- Test: unit for `decide_after_round` with `delivery_mode=chat_completions`
- Test: integration — a `continue_once` (or delayed deterministic) Completions
  Run does not go `queued` mid-window; a tiny `sync_timeout_seconds` ends
  `paused(compat_timeout)`; Scheduler cancels one older than 24 hours

- [ ] **Step 1: Failing tests**

- [ ] **Step 2: Flag the Run at creation; Worker skips `SLICE_ENDED` for ordinary
  round/slice expiry while the window is open; still honours pause/cancel/limit
  /completion. On window expiry: pause request then `SAFE_PAUSE_REACHED` with
  `PauseReason.COMPAT_TIMEOUT`. Scheduler: `compat_timeout` + 24h → cancel.**

- [ ] **Step 3: Completions wait loop maps that pause to `requires_runs_api` or
  a dedicated timeout error as product §18.1: timeout is `paused(compat_timeout)`
  and the HTTP/SSE error is the compat timeout path, not a silent 200.**

- [ ] **Step 4: Commit**

`feat: a completions run holds its lease until it finishes or the minute is up`

---

### Task 9: Streaming Completions

**Files:**
- Modify: `runs/infrastructure/openai_model.py`, deterministic provider
- Modify: Completions route
- Test: streamed chunks; error event after headers when the Run pauses

- [ ] **Step 1: Failing tests with `stream: true`**

Deterministic provider emits at least one `delta` chunk then a stop. A Run that
pauses after headers yields an `error` SSE event and ends. Idempotent replay of
a streamed request streams.

- [ ] **Step 2: Provider `complete` grows a stream method or the Completions
  adapter subscribes to incremental text without writing RunEvents per token**

- [ ] **Step 3: Commit**

`feat: completions can stream tokens without turning them into run events`

---

### Task 10: `shell.exec` records an Artifact

**Files:**
- Modify: `tools/application/execute.py`, Worker execute_stream path
- Test: unit/integration that output over the inline cap sets `artifact_id` in
  the tool result and `GET /api/v1/artifacts/{id}/content` returns the bytes
- Modify: `scripts/workspace_drill.py` only if a small assertion is cheap; the
  full download scenario can wait for 4B

- [ ] **Step 1: Failing test**

- [ ] **Step 2: Thread `ArtifactRecorder` through `execute_stream`**

Ceilings stay enforced while bytes arrive (already true on the recorder).

- [ ] **Step 3: Commit**

`feat: long shell output is an artifact, not a truncated shrug`

---

### Task 11: 4A verification record and docs

**Files:**
- Create: `docs/superpowers/verification/2026-08-13-m1-machine-identity.md`
- Modify: `docs/development.md` (keys, Completions curl)
- Modify: `docs/superpowers/plans/2026-08-10-tiny-hermes-m1-roadmap.md` if the
  slice pointer is still stale

- [ ] **Step 1: CI green on the 4A commits**

- [ ] **Step 2: Write the record: hashes, routes, the 409-creates-zero-rows
  proof, the snapshot example, leftover (Playground, Secrets, §24.1)**

- [ ] **Step 3: Commit**

`docs: record what machine identity and completions proved`

---

## 3. Slice 4A completion checklist

- [ ] Blocked Runs API snapshot matches technical design §13.2.
- [ ] API Key plaintext is returned once; listing never includes it.
- [ ] Caller on Sessions/Runs/idempotency is the ServiceAccount, not the key.
- [ ] Key ∩ role is enforced; header cannot switch workspace.
- [ ] Completions default Session is ephemeral; explicit header is persistent.
- [ ] Completions 409 `session_blocked` inserts no Run.
- [ ] Sync Run does not requeue on an ordinary slice boundary inside the window.
- [ ] `paused(compat_timeout)` exists and ages out.
- [ ] Streamed errors after headers are an SSE `error` event.
- [ ] Long `shell.exec` output is a tenant-scoped Artifact.
- [ ] Published Agent content hashes from phases 2–3C are unchanged.

## 4. Out of this plan

4B console (Playground, tools UI, i18n), 4C Secrets/KEK, 4D benchmarks and
Feishu. Do not start them on this plan's branch.
