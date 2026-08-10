# M1 Run Foundation Design

> Date: 2026-08-10
>
> Status: written design for user review
>
> Delivery slice: M1 phase 2A of three

## 1. Purpose and authority

This slice establishes the Agent, Session, and Run business rules and their PostgreSQL transaction guarantees before long-running processes are introduced.

The following documents remain authoritative for product behavior:

- `docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` v2.4;
- `docs/superpowers/specs/2026-08-09-tiny-hermes-m1-technical-design.md` v1.1;
- `docs/superpowers/plans/2026-08-10-tiny-hermes-m1-roadmap.md` phase two.

This document does not weaken or replace them. It narrows phase two into a mergeable first batch and fixes the seams that phase 2B will use.

## 2. Observable outcome

After phase 2A, an authenticated workspace administrator or developer can use the management API to:

1. create a stable Agent;
2. edit its one mutable Agent Draft with revision checking;
3. publish immutable Agent Versions and point the Agent back to an earlier version without changing history;
4. create a persistent Session for a published Agent;
5. submit Runs with an `Idempotency-Key` and receive either the newly created snapshot or the original replayed snapshot;
6. list and inspect Runs, including queue position and server-computed available actions;
7. pause, resume, or cancel states that can be handled without a Worker;
8. derive a replay-safe retry from a failed Run while sharing the original budget and retry limit.

Runs remain queued in this slice because there is no Worker process. Unit and PostgreSQL integration tests exercise transitions that normally come from Worker and Scheduler signals without adding test-only HTTP routes.

## 3. Explicit non-goals

Phase 2A does not deliver:

- a Worker or Scheduler process loop;
- Redis wake-up delivery;
- SSE streaming or event retention cleanup;
- a real or deterministic model call;
- tools, files, WorkspaceRevision, Artifact, or sandbox behavior;
- ServiceAccounts, API keys, EndUsers, Chat Completions, or Feishu;
- Agent Builder, Playground, or Run Detail pages.

Phase 2B will add Worker, Scheduler, SSE, Redis wake-up, and the deterministic model substitute. Phase 2C will add the minimum pages, browser acceptance, restart scenarios, and updated operator documentation.

## 4. Domain modules and seams

### 4.1 Agent Catalog module

The Agent Catalog module hides draft revision checks, normalized configuration, content hashing, version numbering, publication, rollback, and workspace ownership behind a small application interface:

- create and read Agents;
- replace a Draft when its expected revision matches;
- publish the current Draft;
- activate an existing Agent Version.

HTTP routes and tests use the same interface. The PostgreSQL adapter performs publication and pointer updates atomically. There is no generic CRUD repository exposed to callers.

An Agent Version snapshot uses schema version `1` and contains only phase-two fields:

```json
{
  "schema_version": 1,
  "personality": "You are a concise enterprise assistant.",
  "model_policy": {
    "provider": "deterministic",
    "scenario": "complete"
  },
  "tools": [],
  "limits": {
    "max_execution_seconds": 900,
    "max_elapsed_seconds": 86400,
    "max_model_calls": 20,
    "max_tool_calls": 50,
    "max_derived_retries": 3
  }
}
```

Publication rejects unknown fields, a non-deterministic provider, non-empty tools, invalid limits, or a Draft that changed after the caller read it. Normalization uses sorted JSON keys and stable UTF-8 encoding; the version content hash is SHA-256 of those normalized bytes.

Publishing requires the caller's expected Draft revision. When the normalized Draft hash already equals the Agent's current version, publication returns that version as unchanged and does not allocate another version number. Concurrent publication locks the Agent and Draft, so different accepted contents receive unique increasing version numbers while identical contents do not create duplicates. Activating an earlier version changes only the Agent pointer and writes an audit event.

### 4.2 Run Coordination module

The Run Coordination module owns all Session and Run invariants. Its application interface accepts business commands rather than exposing table-level mutations:

- create a Session;
- accept an idempotent Run;
- inspect and list Runs;
- apply a state signal or a user control request;
- allocate one or more Run Event sequences;
- derive a safe retry;
- claim a queued Head Run lease;
- repair a Session head.

The PostgreSQL adapter implements each operation as one explicit transaction. API routes, the future Worker, and the future Scheduler cannot update Run state columns directly.

### 4.3 Authentication and workspace selection

Browser authentication remains cookie based. `X-Workspace-Id` selects the workspace for every Agent, Session, and Run collection request; the server then verifies membership and resource ownership.

Workspace administrators and developers may manage Agent Drafts, publish or activate versions, create Runs, and control Runs. Viewers may list and inspect but cannot create or control. A platform administrator acting outside membership may use platform authority only where the existing product matrix allows it, and the action is audited.

Repeated login, CSRF, workspace-header parsing, and current-user resolution move into shared presentation dependencies. Authorization itself remains in the application transaction so a caller cannot pass a valid header for a resource owned by another workspace.

## 5. Data model

One additive migration creates these tables without rewriting the phase-one migration:

| Table | Purpose and key constraints |
|---|---|
| `agents` | Workspace-owned stable identity; unique workspace alias; nullable current version pointer |
| `agent_drafts` | One row per Agent; JSON spec; positive revision; last editor and update time |
| `agent_versions` | Immutable numbered snapshots; schema version, normalized JSON, content hash, publisher; unique Agent/version number |
| `sessions` | Workspace, Agent, mode, caller type/id, Head Run, next Run and message sequences, nullable workspace revision marker |
| `session_messages` | Canonical JSON message ordered uniquely within a Session; source Run and redaction flag |
| `runs` | Agent Version, status fields, optimistic state version, next event sequence, Session order, blocking, retry root/source, checkpoint and times |
| `run_budget_scopes` | One row per root Run; limits, consumption, derived retry count, optimistic version |
| `run_events` | Immutable payload; unique Run/sequence |
| `worker_leases` | At most one current lease row per Run; owner, acquisition, expiry, release, and version |
| `idempotency_records` | Unique workspace/caller/endpoint/key; request fingerprint, response snapshot, Run and expiry |

Foreign keys include workspace ownership wherever the referenced table carries a workspace. Cross-workspace relationships are rejected both by application checks and composite database constraints where PostgreSQL can enforce them directly.

Strings that represent Run status, pause reason, caller type, session mode, and event type use database check constraints matching the domain enums. Times are stored in UTC. JSON payloads contain versioned, validated objects rather than arbitrary Python serialization.

## 6. Run invariants

### 6.1 Session order

- `sessions.head_run_id` is the smallest `session_sequence` among non-terminal Runs, or null when none exist.
- Only the Head Run may be claimed.
- Every later non-terminal Run points `blocked_by_run_id` to the current Head Run.
- Terminal Runs can never become Head Run.
- Run terminalization and Head Run handoff occur while the Session row is locked in the same transaction.

The regression case `Run1 executing, Run2 cancelled, Run3 queued` must hand the Session directly from Run1 to Run3.

### 6.2 State machine

The complete product-design v2.4 state matrix is represented by a pure `RunStateMachine`. Callers submit a typed signal such as lease acquired, execution slice ended, pause requested, cancellation checkpoint reached, approval resolved, external condition resolved, completed, failed, or interrupted. The state machine returns the allowed mutation; callers do not choose a target state directly.

User controls have these phase-2A observable effects:

- queued pause becomes `paused(manual)` immediately;
- running pause records `pause_requested_at` and waits for a future checkpoint signal;
- paused resume becomes queued only when the shared budget permits it;
- queued, waiting, paused, or interrupted cancellation can become cancelled immediately;
- running cancellation records `cancel_requested_at`; a future Worker decides at a safe checkpoint whether cleanup requires `cancelling`;
- every illegal request is rejected and writes an audit denial without changing Run state.

Every accepted state change compares `state_version` and increments it once. A stale caller receives `409 state_version_conflict` with the current Run snapshot URL.

### 6.3 Events

All writers use one event-sequence allocator. Reserving `n` values atomically increments `runs.next_event_sequence` by `n` and returns the reserved contiguous range. No code may calculate `max(sequence) + 1`.

State, event, budget, Session head, and audit changes that describe one business operation commit together. Phase 2A exposes event metadata through Run snapshots for diagnostics but reserves the streaming event route for phase 2B.

### 6.4 Idempotent Run acceptance

Run creation first inserts the idempotency record with `INSERT ... ON CONFLICT DO NOTHING`. The unique identity is:

```text
workspace_id + caller_type + caller_id + endpoint + idempotency_key
```

The fingerprint is SHA-256 of the normalized caller-supplied request method, route identity, selected workspace, Session, input message, and requested limit overrides. Server-derived values such as the Agent's current version are not fingerprint inputs; a network retry must still return the original Run after a later publish or rollback. Equal fingerprints return the original Run with `200`, `Idempotent-Replayed: true`, and no duplicate message, event, budget, or audit side effect. A different fingerprint returns `409 idempotency_key_reused`.

The first successful creation returns `201` and `Location`. The transaction locks the Session, allocates its Run and message sequences, fixes the Agent Version, creates the root budget, stores the user message, establishes Head or pending status, writes the first Run Event and audit event, and finally stores the response snapshot.

An idempotency record cannot expire while its Run is non-terminal. Terminal handoff sets its expiry to the terminal time plus 24 hours; phase 2B Scheduler cleanup later removes expired records. Phase 2A keeps them instead of adding a fake cleanup process.

### 6.5 Shared retry budget

A root Run points `budget_root_run_id` to itself. A Derived Retry points to the same root and increments `derived_retry_count` on the single locked `run_budget_scopes` row.

The retry route also requires an `Idempotency-Key`; retry idempotency uses the source Run and caller-supplied request body, while the retry-count increment and new Run creation remain in the same transaction.

Retry requires all of the following:

- source status is failed;
- source is the latest Run in its Session;
- the last checkpoint is complete and `replay_safe=true`;
- no external side effect is unknown;
- the Session workspace revision marker still equals the checkpoint marker; both are null before phase three file support;
- elapsed, execution, model-call, tool-call, token, and retry-count budgets have remaining capacity.

Concurrent retry requests serialize on the root budget row. At most three Derived Retries are created by default across the whole retry chain. A successful retry is a new Run in the original Session and never reopens or mutates the failed source Run.

## 7. HTTP interface for phase 2A

All workspace-scoped routes require authentication and `X-Workspace-Id`; writes also require the existing CSRF header.

```text
GET    /api/v1/agents
POST   /api/v1/agents
GET    /api/v1/agents/{agent_id}
GET    /api/v1/agents/{agent_id}/draft
PUT    /api/v1/agents/{agent_id}/draft
GET    /api/v1/agents/{agent_id}/versions
POST   /api/v1/agents/{agent_id}/publish
POST   /api/v1/agents/{agent_id}/rollback

GET    /api/v1/sessions
POST   /api/v1/sessions
GET    /api/v1/sessions/{session_id}

GET    /api/v1/runs
POST   /api/v1/runs
GET    /api/v1/runs/{run_id}
POST   /api/v1/runs/{run_id}/pause
POST   /api/v1/runs/{run_id}/resume
POST   /api/v1/runs/{run_id}/cancel
POST   /api/v1/runs/{run_id}/retry
```

Run snapshots include status, state version, fixed Agent Version, Session order, blocking information, shared budget summary, last event sequence, and server-computed `available_actions`. A pending Run created behind a paused or waiting Head Run still returns success and includes the required `queue.status=session_blocked` explanation.

Problem Details codes include `workspace_required`, `forbidden`, `agent_not_found`, `draft_revision_conflict`, `agent_not_published`, `session_not_found`, `run_not_found`, `idempotency_key_required`, `idempotency_key_reused`, `invalid_state_transition`, `state_version_conflict`, `retry_not_safe`, `retry_context_stale`, `retry_budget_exhausted`, and `retry_limit_reached`. Responses never reveal the existence or name of a resource in another workspace.

## 8. Failure and transaction behavior

- Database failure rolls back the complete business operation; Redis is not involved in this slice.
- Expected denied controls and invalid transitions save a redacted audit denial, then return their application error. The request dependency commits only this explicit audited-denial exception class; unrelated exceptions still roll back.
- A unique event-sequence conflict rolls back the state change. The application rereads and retries at most three times, then returns `event_sequence_conflict`.
- A stale Draft revision, Run state version, or budget version never receives an automatic last-write-wins update.
- API responses contain no Agent personality text in error context and no raw CanonicalMessage or checkpoint content unless the authorized resource response explicitly requests it.

## 9. Verification strategy

### 9.1 Fast domain tests

- every allowed and forbidden state signal in the v2.4 matrix;
- pause and cancellation request semantics at safe and unsafe points;
- server-computed available actions;
- Agent Version normalization, hashing, immutability, and Draft revision conflicts;
- budget remaining calculations and the shared three-retry limit;
- idempotency fingerprint stability.

### 9.2 PostgreSQL integration tests

- the additive migration upgrades, downgrades, and recreates all constraints;
- concurrent Agent publication allocates unique increasing version numbers;
- equal concurrent Idempotency Keys create exactly one message, Run, budget scope, first event, and audit success;
- the same key with a different fingerprint returns a conflict;
- API, claim, and repair writers concurrently reserve unique contiguous Run Event sequences;
- two claim attempts produce one valid Worker Lease;
- terminal handoff skips an already cancelled pending Run;
- a deliberately corrupted Head Run is repaired and audited;
- concurrent retries across one root budget create no more than three Derived Retries;
- workspace header mismatch and cross-workspace identifiers reveal no protected fields.

### 9.3 HTTP flow

One integration flow creates a workspace, creates an Agent, updates and publishes its Draft, creates a Session, creates three Runs, cancels the second, terminalizes the first through the application seam, and observes the third as Head Run. Replaying the first create request returns the original Run.

No test-only route, fake successful HTTP response, or in-memory substitute is used for this flow.

## 10. Exit criteria and next seams

Phase 2A is ready to merge only when:

- all phase-one checks still pass;
- the phase-2A domain, migration, concurrency, permission, and HTTP tests pass on PostgreSQL;
- the Run state machine is the only production path that decides Run state changes;
- no route or adapter performs table-level state mutation outside the Run Coordination module;
- migrations pass upgrade, `alembic check`, downgrade, and upgrade again;
- Agent and Run operations are workspace-isolated and audited;
- the repository contains no Worker, Scheduler, SSE, model, sandbox, or UI placeholder pretending to be operational.

Phase 2B consumes the fixed Run Coordination interface to build the Worker claim loop, lease renewal, deterministic model scenario, Scheduler scans, Redis wake-up, and SSE continuation. Phase 2C consumes the stable HTTP snapshots and event stream rather than reading database tables or inventing client-side state.
