# tiny-hermes M1 Phase 4 Product Closure

> Date: 2026-08-13
>
> Status: written design for user review
>
> Inputs: product design v2.4, M1 technical design v1.1, phases 1–3C as merged
> on `main` (`5b28b96`)

## 1. Why this phase exists

Phases 1–3C delivered a platform that can start, run a Session, talk to a real
model, execute tools inside a locked-down sandbox, and persist
`/workspace/data` as immutable revisions. A developer still cannot:

- bind tools from the console and test the Agent in a Playground;
- call the platform with an API Key;
- use an OpenAI-compatible Chat Completions client;
- store a Secret the platform can unwrap;
- point a reviewer at a Linux reference install that meets product design
  §24.1 and §27.1.

That is the whole of M1 that is still missing. This phase closes it. It does
not start M2.

## 2. Observable outcome

After phase 4:

1. A workspace developer publishes an Agent with tools, tests it in Playground
   against a persistent Session, and sees status, tool rounds, files, and
   Artifacts without leaving the console.
2. The same developer issues an API Key bound to a ServiceAccount. The
   plaintext is shown once. A later listing shows only prefix and metadata.
3. An API client with that key creates Runs and streams events. The key cannot
   switch Workspace via `X-Workspace-Id`.
4. `POST /v1/chat/completions` maps `model` to a published Agent alias. A
   request without a session header creates an ephemeral Session. An explicit
   `X-Tiny-Hermes-Session-Id` reuses a persistent one. A blocked persistent
   Session returns 409 `session_blocked` and creates no Run.
5. A Chat Completions Run that later needs approval, external wait, or an
   honest pause returns `requires_runs_api` with the Run ID. A Run that hits
   the 60-second sync window pauses as `paused(compat_timeout)`.
6. A Secret is stored as ciphertext under a wrapped DEK. The API never returns
   plaintext after create. A KEK rewrap can be interrupted and resumed.
7. Product design §27.1's thirteen scenarios and §24.1's reference benchmarks
   have evidence on a Linux Docker host. A Feishu WebSocket investigation
   record exists; no Feishu adapter ships.

## 3. Explicit non-goals

- No Approvals product, Usage dashboard, Audit query/export, skill market,
  MCP/OpenAPI tools, sub-Agent task tree, or end-user `chat-web` (M2/M3).
- No Feishu adapter, webhook handler, or channel binding.
- No OIDC, EndUser, or ExternalIdentity rows.
- No claim that `/workspace/data` is a host-disk hard quota (3C already named
  this).
- No XFS/ext4 project quotas.
- No token-level RunEvents. Partial model text is a delivery concern of Chat
  Completions; the transcript still records complete assistant turns.
- No Monaco/YAML Agent editor in M1. The typed form remains the builder.
- No platform-admin API Key that spans workspaces.

## 4. What `main` already proves

These are seams, not work:

| Area | On `main` | Still missing |
|---|---|---|
| Browser auth | Cookie + CSRF | API Key, ServiceAccount |
| Caller identity | `CallerType.SERVICE_ACCOUNT` is in the check constraint; only `USER` is populated | The table and the auth path |
| Runs API | Create, control, retry, SSE, idempotency, FIFO | Queue snapshot head fields |
| `session_blocked` | Pending Run is created; `queue.status` is set | `head_status`, `head_reason`, head `available_actions` |
| Chat Completions | Outbound client to the model provider; no inbound route | Inbound `/v1/chat/completions` |
| Streaming | None; 3A deferred it | CC token SSE; Worker sync-lease hold |
| Console | Bootstrap, workspaces, agent draft/publish, run list/detail, SSE timeline | Playground, tools binding UI, artifacts pane, members write, i18n switch, Secrets/API Keys pages |
| Artifacts | Tenant-scoped metadata and content routes | Recorder not threaded through `shell.exec` |
| Secrets | `credential_ref` is an environment-variable name | Envelope table, KEK, rewrap |
| Membership | `GET …/members` | Invite, role change, remove |
| Feishu | — | Investigation record |
| §24.1 | Workspace-drill envelopes in CI | Formal reference-host benchmark |

`PauseReason.COMPAT_TIMEOUT` already exists on the state machine. The Worker
does not yet emit it.

## 5. Slices

Phase 3 split because the model path and the sandbox path shared no failure
mode. Phase 4 splits because the API product, the console, the crypto, and the
release gate can each be proven without the others — but they share a few
shapes that must land first.

```text
4A  Machine identity and Chat Completions
4B  Console product closure
4C  Secret envelope and KEK rewrap
4D  Acceptance, deploy, Feishu record
```

**4A before 4B.** Playground is a Runs API client. It must consume the completed
queue snapshot and Artifact wiring, not invent a parallel protocol.

**4C after 4A.** API Keys are hashed, not envelope-encrypted. Model endpoints
keep reading `credential_ref` as an environment variable until 4C replaces that
resolver. Chat Completions does not wait on KEK.

**4D last.** Benchmarks and the Feishu record consume a stack that already has
keys, Completions, and a Playground.

Each slice gets its own task-by-task plan after the previous slice's
verification record exists. This document is the shared design. The first plan
is 4A: `docs/superpowers/plans/2026-08-13-m1-machine-identity.md`.

## 6. Shared API shape: the blocked-session snapshot

Technical design §13.2 is not optional decoration. Playground and Chat
Completions both need it, and today's snapshot is short:

```json
"queue": { "position": 2, "status": "session_blocked" }
```

4A widens `RunSnapshot.document()` so `queue` is:

```json
{
  "status": "session_blocked",
  "blocked_by_run_id": "…",
  "position": 2,
  "head_status": "paused",
  "head_reason": {
    "pause_reason": "manual",
    "wait_kind": null,
    "wait_deadline_at": null
  },
  "available_actions": ["resume", "cancel"]
}
```

Rules:

- `queue.available_actions` is the **head** Run's actions for the current
  caller, not this pending Run's. A blocked pending Run still exposes its own
  top-level `available_actions` (cancel of itself). The two lists answer
  different questions and both stay.
- `blocked_by_run_id` remains a top-level field as well as inside `queue`, so
  existing console code does not break.
- For `head` / `pending` / `terminal`, `head_status` and `head_reason` are
  omitted rather than null-padded.
- Chat Completions 409 uses the same structure inside an OpenAI-style error
  envelope, plus `session_id` and `runs_api_url`. It does not create a Run.

This change is a widening of a JSON object. No migration. Existing clients that
only read `queue.status` keep working.

## 7. Slice 4A — machine identity and Chat Completions

### 7.1 ServiceAccount

A ServiceAccount is a workspace-scoped calling principal. It is not a User and
it does not log in with a password.

| Column | Rule |
|---|---|
| `id` | Stable `caller_id` when `caller_type=service_account` |
| `workspace_id` | Binding; cannot move |
| `name` | Unique per workspace |
| `role` | `developer` or `viewer` — the same `Role` enum, never `workspace_admin` |
| `status` | `active` / `disabled` |
| `created_by_user_id` | Audit trail |

Disabled accounts reject every authenticated request. Existing Sessions they
created remain; new Runs are refused.

Workspace admins create, disable, and list them. Developers can list. Viewers
cannot. Platform admins acting in a workspace are audited as today.

### 7.2 API Key

| Column | Rule |
|---|---|
| `id` | Internal |
| `service_account_id` | Owner; the caller identity is the account, never the key |
| `token_digest` | SHA-256 of the full token |
| `prefix` | First 8 characters of the public token, unique enough to find in a list |
| `scopes` | Frozen tuple, see below |
| `agent_ids` | Optional allow-list; empty means every Agent in the workspace |
| `expires_at` | Nullable |
| `revoked_at` | Nullable |
| `created_at` | |

The plaintext token is `thk_` + 32 random bytes, hex-encoded. Create returns it
once in the JSON body. Every later read returns `prefix`, scopes, expiry,
revoked state, and timestamps.

Verification: look up by prefix, compare digest, reject revoked/expired/disabled
account. The request then carries `CallerIdentity(SERVICE_ACCOUNT, account.id)`.

**Scopes (M1 closed set):**

| Scope | Permits |
|---|---|
| `runs.read` | GET sessions, runs, events, artifacts |
| `runs.write` | POST sessions, runs, Chat Completions |
| `runs.control` | pause, resume, cancel, retry |
| `agents.read` | GET agents, versions, published alias lookup |

A key's effective permission is the **intersection** of the ServiceAccount's
role and the key's scopes. A viewer account cannot be given `runs.write` by
minting a key. A developer account with only `runs.read` cannot create Runs.

Browser cookie auth is unchanged: CSRF stays, no Bearer. API Key auth is
`Authorization: Bearer thk_…`. CSRF does not apply. If `X-Workspace-Id` is
present it must equal the account's workspace; if absent, the workspace is
taken from the binding (technical design §12.2).

Idempotency stays keyed by `(workspace_id, caller_type, caller_id, endpoint,
idempotency_key)`. Rotating a key does not split a caller's idempotency scope.

Audit `actor_type` gains `service_account`. Model-endpoint admin writes, still
browser-only, start writing AuditEvents in 4B/4C when those pages exist; 4A
does not add that UI.

### 7.3 Chat Completions

Route: `POST /v1/chat/completions` (no `/api` prefix — OpenAI client
compatibility). Auth: API Key only. Cookie sessions are refused here so a
browser cannot accidentally skip CSRF by posting to the compatibility surface.

**Mapping:**

- `model` → published Agent alias in the key's workspace.
- The AgentVersion must have `delivery.chat_completions.enabled = true`.
- `sync_timeout_seconds` on that delivery block is 1–60, default 60.
- Tools that would require M2 approval are not in M1; any Agent that binds only
  implemented tools may enable the mode.
- Request body `user` is ignored as a session key.
- Default: create `session_mode=ephemeral`, one Run, return the completion.
- `X-Tiny-Hermes-Session-Id`: must name an existing persistent Session in this
  workspace, for this Agent, owned by this caller. Failure is 404-shaped OpenAI
  error, never a silent fallback to ephemeral.

**Two `session_blocked` flavours** (product §18.1):

| Path | HTTP | Creates Run? |
|---|---|---|
| Runs API / Playground, head paused or waiting | 201 + snapshot `queue.status=session_blocked` | yes |
| Chat Completions, explicit persistent Session, head paused or waiting | 409 `session_blocked` | no |

**`requires_runs_api`:** the Run was created and then entered `waiting_*` or
`paused` for a reason other than `compat_timeout`. The compatibility response
stops. The Run remains and is controllable through Runs API.

**`paused(compat_timeout)`:** the sync window elapsed. The Worker records a
pause request, finishes the in-flight checkpoint step, then pauses. Scheduler
already has a wait-timeout scan; 4A extends it so a `compat_timeout` pause older
than 24 hours becomes `cancelled`.

**Sync lease:** while a Chat Completions Run is inside its window, `decide_after_round`
does not emit `SLICE_ENDED` for an ordinary goal-round or `max_slice_seconds`
boundary. It still emits pause, cancel, limit, completion, and failure. The
WorkerLease is renewed as today. This is a flag on the Run (`delivery_mode=
chat_completions` or equivalent), not a second state machine.

**Streaming:** `stream: true` uses OpenAI chunk SSE for assistant text and a
terminal chunk. After headers are sent, a `requires_runs_api` or
`compat_timeout` outcome is an `error` SSE event, then the connection ends.
`stream: false` buffers the completed assistant text. The Worker today is
non-streaming; 4A adds streaming on the OpenAI provider adapter and a sink the
Completions route reads. Deterministic provider can emit its whole text as one
chunk.

Partial tokens are **not** written as RunEvents. The Session transcript still
receives the complete assistant CanonicalMessage at the checkpoint.

**Idempotency:** `Idempotency-Key` required, same store, endpoint name
`POST /v1/chat/completions`. Fingerprint includes model, messages, session
header, and `stream` (a streamed replay must stream).

### 7.4 AgentSpec widening

`AgentSpec` stays `schema_version=1`. Add an optional delivery block with
defaults that preserve every existing content hash:

```python
class ChatCompletionsDelivery(BaseModel):
    enabled: bool = False
    sync_timeout_seconds: int = Field(default=60, ge=1, le=60)

class AgentSpec(...):
    delivery: ChatCompletionsDelivery = ChatCompletionsDelivery()
```

An unbound spec already omitted the field; the default must serialize the same
bytes as today, or published hashes break. The implementation plan pins this
with the same kind of hash-stability test tools used.

### 7.5 Artifact recorder wiring

3C built the recorder and the routes, then left `shell.exec` returning capped
inline output. 4A threads the recorder through `execute_stream` so a long
command produces `artifact_id` / `artifact_truncated` in the tool result text
(never a MinIO key). Playground and Run Detail in 4B only display what this
wiring already stored.

The volume-orphan label sweep stays a 4D hardening item. Teardown already
removes volumes with their sandboxes.

## 8. Slice 4B — console

The web app at `apps/web` is the phase-2C shell plus 3A model-endpoint
selection. 4B extends it; it does not replace it.

### 8.1 Agent Builder

Keep the typed form. Add:

- Tools: a checklist of `IMPLEMENTED_TOOLS` (`file.list` / `file.read` /
  `file.write` / `shell.exec`). Saving `tools: []` remains legal. The current
  static `toolsPhaseGap` copy goes away.
- Delivery: the Chat Completions enable flag and sync timeout, default off.
- Diff: draft vs current published version, field-level, before publish.
- Rollback: call the existing `POST /agents/{id}/rollback`.
- Name/alias edit through the existing agent update path if present; otherwise
  add the missing PATCH.

No memory, skills, sub-agents, or channels. No Monaco.

### 8.2 Playground

New route under the Agent: `/workspaces/:id/agents/:agentId/playground`.

It is a Runs API client with a cookie, not a Chat Completions client.

- Creates or selects a **persistent** Session for this Agent and caller.
- Composer submits `input` as today, with a fresh Idempotency-Key per send.
- Live panel: existing `useRunEvents` plus the completed assistant/tool
  messages once they are in the Session transcript. Token SSE is not required
  here (see §3).
- Status, `pause_reason`, queue snapshot from §6, and buttons only from
  `available_actions` (this Run and, when blocked, the head list in `queue`).
- Files: list via `file.list` results and Artifact download links from tool
  results / artifact routes. No in-browser editor of `/workspace/data`.
- New Session action starts an unrelated persistent Session.

When the head is paused, sending still creates a pending Run (201) and the UI
renders the queue snapshot — it does not pretend Chat Completions' 409.

### 8.3 Run Detail

Keep summary, budget, timeline, controls. Add:

- Messages: CanonicalMessage list for the Session, redacted as stored.
- Tools: call name, argument summary, result preview, artifact link.
- Files: Artifact list for the Run; download uses the existing content route.
- Event payload: folded JSON, not the default view.

Still no parent/child tree, compression events, or pricing UI. Tests that
assert those labels absent stay, retargeted to the M2/M3 labels only.

### 8.4 Other pages

- Members: invite by email of an existing User, change role, remove.
  Workspace-admin only. No user-registration API; inviting an unknown email is
  a clear error, not an implicit signup.
- API Keys and ServiceAccounts: create/disable/revoke; plaintext shown once
  in a dismissible panel.
- Model endpoints: platform-admin register, check, enable/disable — the API
  exists; the console does not call it yet.
- Secrets: 4C's UI, listed here so the nav in technical design §15.1 is one
  tree. 4B may show a stub that 4C replaces, or wait and ship the page in 4C.
  Prefer waiting: a Secrets page that cannot store a Secret is a fake.

### 8.5 i18n and chrome

Wire `en-US.ts`, a locale provider, a switcher, and locale-aware dates.
Persisted light/dark toggle (today follows `prefers-color-scheme` only).
Keyboard paths through Playground and Builder stay covered by component tests
plus Playwright.

Run list pagination: add `limit`/`cursor` on `GET /api/v1/runs` if the 4B
plan's first list-page test shows the unpaginated payload is already a
problem. Do not paginate preemptively.

## 9. Slice 4C — Secret envelope

Replaces the 3A environment-variable credential resolver without changing the
outbound safety client.

- Table `secrets`: ciphertext, wrapped DEK, `key_id`, workspace or platform
  scope, name, status. No plaintext column.
- Create returns name, scope, timestamps, and a mask. No read-plaintext route.
- KEK from deployment config (`TINY_HERMES_KEK` or equivalent). Missing KEK
  fails ready for the API process that serves Secret writes; Workers that only
  unwrap at call time fail the call, not process boot, if a Secret is needed.
- `ModelEndpoint.credential_ref` becomes a Secret id **or** remains an env var
  name during a documented overlap: an endpoint whose ref does not match a
  Secret id is resolved as today. 4C's verification record must say which
  endpoints in Compose use which form. Local Compose should move to a seeded
  Secret so the rewrap drill has something to wrap.
- Rewrap job: scans rows with old `key_id`, unwraps with the previous KEK,
  wraps with the new one, records progress. Interrupt and rerun. Product §23
  row 11: a database backup without the KEK cannot decrypt.

API Keys stay hashed. They are not Secrets.

## 10. Slice 4D — acceptance

### 10.1 Product §27.1

Each of the thirteen scenarios gets either an automated check (Playwright,
drill, pytest) or a dated manual record in
`docs/superpowers/verification/2026-08-13-m1-product-closure.md`. Scenarios
already proven in 2A–3C (idempotency, outbound refusal, retry budget, restart
drill, workspace persistence) are linked, not re-litigated, and re-run as
regression.

### 10.2 Product §24.1

A `scripts/benchmark_m1.py` (name flexible) against the documented Linux
reference shape: 8 vCPU, 16 GB, local SSD, 50 ms deterministic delay, image
cached, 100k preloaded RunEvents. Raw P50/P95/P99, error rate, CPU, RSS, and
git SHA go in the verification record. Failures are not fixed by editing the
threshold.

3C's workspace-drill numbers are early gates, not this benchmark.

### 10.3 Deploy and upgrade

Linux reference compose, generated secrets, upgrade-from-previous-migration
CI (already partly present), and a short operator path in `docs/development.md`
that actually creates an Agent, runs Playground, and calls Completions with a
key.

### 10.4 Feishu

A laboratory note, not a product: app types and event types the official
WebSocket channel actually delivers; reconnect behaviour; whether events missed
during a disconnect are replayed. Explicit columns: measured / unconfirmed /
Webhook-fallback decision. No adapter code.

### 10.5 Hardening leftover

Label-enumerated volume GC, if cheap once the Controller already lists by
label. Not a 0.1 blocker: an orphan requires a crash in teardown's last step.

## 11. Auth and role matrix (M1)

| Action | viewer | developer | workspace_admin | API Key |
|---|---|---|---|---|
| Read runs/sessions/artifacts | yes | yes | yes | `runs.read` |
| Create session/run / Completions | no | yes | yes | `runs.write` |
| pause/resume/cancel/retry | no | yes | yes | `runs.control` |
| Read agents | yes | yes | yes | `agents.read` |
| Edit draft / publish | no | yes | yes | no |
| Members, ServiceAccounts, Keys | no | list SA/keys | yes | no |
| Secrets | no | list names | write | no |
| Model endpoints | list selectable | list selectable | list selectable | no |
| Register model endpoint | platform admin only | | | no |

Platform admins keep the existing membership bypass, always audited.

## 12. Failure modes worth pinning

| Event | Outcome |
|---|---|
| API Key revoked mid-SSE | Stream ends; no further chunks |
| ServiceAccount disabled | 401/403 on the next request; in-flight WorkerLease runs to the next checkpoint |
| Completions Agent not enabled for CC | OpenAI error `agent_not_compatible` |
| Completions Agent alias missing | OpenAI error `model_not_found` |
| Completions hits pause(limit) | `requires_runs_api` (limit is a pause the compat client cannot continue) |
| Completions hits quota rollback | same: the Run is `paused(limit)`; client is told to use Runs API |
| Key with `runs.write` but viewer role | 403 at the intersection check |
| `X-Workspace-Id` disagrees with key binding | 403, generic, no existence leak |
| Secret unwrap with wrong KEK | call fails; row unchanged |
| Rewrap interrupted | last completed row stays new `key_id`; job resumes |

## 13. Tests this design requires (by slice)

**4A.** Queue snapshot contract; ServiceAccount/API Key lifecycle; scope ∩ role;
key cannot switch workspace; Completions ephemeral vs persistent header;
Completions 409 does not insert a Run; Runs API 201 still does; sync Run does
not `SLICE_ENDED` inside the window; `paused(compat_timeout)` then Scheduler
cancel at 24h; streamed error after headers; artifact_id on a long
`shell.exec`; hash stability of default `AgentSpec`.

**4B.** Playwright: draft tools, Playground send, blocked-session UI, artifact
download, locale switch, rollback. Component tests keep "no task tree / no
approvals nav".

**4C.** Encrypt/decrypt round-trip; backup without KEK cannot unwrap; rewrap
resume; model call reads the new Secret form.

**4D.** Benchmark script gates; drill that a generated-secret compose boots;
Feishu note present with the three columns.

## 14. Documentation this phase must update

- product design §§18.1–18.3, 20.1–20.3, 24.1, 27.1 (no silent contradiction);
- technical design §§6.1–6.2, 12–15, 18–20;
- roadmap phase four (point at this design and the slice plans);
- `docs/development.md` (keys, Completions, Playground, Secrets);
- 3C verification record stays historical; 4A–4D each get their own.

## 15. Exit criteria

Phase 4 is done when every slice's verification record is written and:

- [ ] §27.1 scenarios 1–13 have automated or dated evidence.
- [ ] §24.1 thresholds pass on the Linux reference shape with raw output saved.
- [ ] Runs API, SSE, console, and Chat Completions agree on one Run's state.
- [ ] API Key plaintext is returned once; listing never returns it.
- [ ] Completions 409 `session_blocked` creates zero Runs.
- [ ] Secret rewrap resumes; a backup without the KEK cannot decrypt.
- [ ] Feishu record separates measured, unconfirmed, and the Webhook decision.
- [ ] Phases 1–3C checks still pass.
- [ ] Public description remains "单 Agent 安全运行骨架".

0.1 Technical Preview is a gate **after** this phase, not a slice inside it.
