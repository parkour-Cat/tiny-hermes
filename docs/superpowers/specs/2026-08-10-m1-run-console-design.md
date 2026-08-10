# M1 Run Console Design

> Date: 2026-08-10
>
> Status: written design for user review
>
> Delivery slice: M1 phase 2C of three

## 1. Purpose and authority

Phase 2A fixed the Agent, Session, and Run rules. Phase 2B made Runs execute on
their own and exposed a resumable event stream. Everything phase two promises is
now reachable over HTTP, and nothing is reachable through a browser: the console
still stops at the Workspace list.

This slice adds the smallest console that can drive the phase-two chain end to
end — draft an Agent, publish it, submit a Run, watch its events, control it —
and it closes the one phase-two exit check that phase 2B left unproven.

The following documents remain authoritative:

- `docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` v2.4,
  especially §20 (UI) and §27.1 (M1 acceptance scenarios);
- `docs/superpowers/specs/2026-08-09-tiny-hermes-m1-technical-design.md` v1.1;
- `docs/superpowers/plans/2026-08-10-tiny-hermes-m1-roadmap.md` phase two;
- `docs/superpowers/specs/2026-08-10-m1-run-foundation-design.md` and
  `docs/superpowers/specs/2026-08-10-m1-run-execution-design.md` for the seams
  this slice consumes.

This document does not weaken or replace them. It adds exactly one backend
change (§4); every other backend contract is consumed as committed.

Two roadmap rules govern this slice more than any other, because it is the first
one with a user interface:

- **前端不得用假数据伪装后端能力.** A pane with no phase-two data behind it is
  absent, not empty.
- **所有跨工作空间查询由服务端限定范围，不能依赖界面隐藏.** The console never
  filters for safety. It asks, and the server refuses.

## 2. Observable outcome

After phase 2C, an authenticated developer using only a browser can:

1. pick a Workspace and land on a URL that names it, so a reload or a shared
   link reopens the same scope;
2. create an Agent, edit its draft (personality, model scenario, safety valves),
   and save it;
3. hit a save conflict when the draft moved underneath them, and be told so
   instead of silently overwriting the other edit;
4. publish the draft into an immutable version after an explicit confirmation,
   and see the version number and content hash that resulted;
5. get a readable message — not a 500 — when the alias they chose is taken;
6. submit a Run against the published Agent and see it appear in the Run list
   with its queue position;
7. open a Run and watch events arrive live, with the connection surviving a
   dropped stream and resuming with no gap and no duplicate;
8. see an explicit gap marker when the stream resynchronizes after `410`,
   because a silently shortened timeline is a lie;
9. pause, resume, cancel, and retry a Run through buttons that exist only when
   the server said the action is available;
10. paste another Workspace's Run URL and be refused by the server.

And an operator can:

11. restart the Worker mid-Run and the Redis container while Runs are in flight,
    and confirm from the recorded transcript that no committed state was lost.

Item 11 is the phase-two exit check `Worker 或 Redis 重启不会丢失已提交状态`.
Phase 2B proved lease-abandonment recovery and Redis *unreachability*; it never
restarted a process. Phase two cannot exit until §11 runs.

## 3. Explicit non-goals

Phase 2C does not deliver:

- Playground, chat-style interaction, or message composition beyond the single
  Run input field;
- the full M1 Agent Builder of product design §20.2 — tools and their
  authorization are phase 3, so the tools section renders as a stated
  phase-3 gap and binds nothing;
- 父子任务树, 上下文和压缩事件, 产物, or Token 和费用 panes from §20.3, because
  no phase-two route produces sub-Runs, compression events, Artifacts, or token
  accounting;
- displaying what the Agent actually produced: no route returns a Run's output
  or its CanonicalMessages, so the console shows state and events, never text
  the model wrote;
- Workspace member management, ServiceAccounts, API keys, or Secrets;
- rollback UI — `POST /agents/{id}/rollback` exists but choosing a version to
  roll back to needs the version-diff view that belongs to phase 4;
- pagination, search, or filtering on the Run list (§8.1 states the limit);
- English at runtime, real i18n switching, or a design system beyond Ant Design
  defaults;
- any new backend route, and any backend change other than §4.

## 4. The one backend change: `agent_alias_taken` must actually happen

> Done ahead of the rest of the slice, so the console work starts on a backend
> that already answers correctly. Details below describe what was built.

`POST /api/v1/agents` with a taken alias currently returns **500**.
`AgentAliasAlreadyUsed` is raised only by the in-memory adapter
(`agents/infrastructure/memory_store.py:41`), so against PostgreSQL the
`uq_agents_workspace_alias` violation escapes untranslated and the 409 branch at
`agents/presentation/routes.py:323` is dead code. Unit tests pass because they
run on the memory adapter; no integration test covers it. It was found by hand
during the phase-2B deployed smoke and recorded as a known limit.

This slice must clear it first, because the Agent creation form has no honest
behavior otherwise: a 500 carries no `code`, so the form could only say
"请求失败" for a mistake the user can fix in two seconds.

The fix is narrow:

- the SQL Agent Catalog translates the unique-violation into
  `AgentAliasAlreadyUsed`, the same exception the memory adapter raises, so both
  adapters satisfy one contract. The translation is keyed to the constraint
  name; any other `IntegrityError` re-raises, so a future constraint cannot be
  silently reported as a taken alias;
- a **PostgreSQL integration test** asserts `409` and `code=agent_alias_taken`,
  since that is the exact gap that let this through;
- a second integration test reuses the alias in a *different* Workspace and
  asserts `201`, because uniqueness is per Workspace and a translation that
  overreaches would be a new bug wearing the old one's clothes;
- the existing memory-adapter unit test stays, so the two adapters are pinned to
  the same behavior from both sides.

Nothing else about agent creation changes. This closes task `6bc77cd0`.

## 5. Workspace scope lives in the URL

Every Agent, Session, and Run route requires `X-Workspace-Id`. The console needs
a selected Workspace, and where that selection lives is a security decision, not
a convenience one.

**Decision: the Workspace ID is a route parameter, and the request header is
derived from it in exactly one place.**

```
/login
/bootstrap
/workspaces
/workspaces/:workspaceId/agents
/workspaces/:workspaceId/agents/:agentId
/workspaces/:workspaceId/runs
/workspaces/:workspaceId/runs/:runId
```

Reasons, in order of weight:

1. A reload, a bookmark, or a link pasted to a colleague reopens the same scope.
   A React-state or `localStorage` selection silently reinterprets the same URL
   differently per browser.
2. Cross-workspace access becomes a *testable* action rather than a hidden
   state. Item 10 of §2 — paste a foreign Run URL, get refused — is only
   expressible if the workspace is addressable.
3. There is one derivation from route parameter to header, so no code path can
   send a header that disagrees with the page the user is looking at.

The console never checks membership itself. It sends the header the URL implies
and renders whatever the server returns, including `403 forbidden` and
`404 agent_not_found`. `workspace_scope_mismatch` and `workspace_required` are
rendered as refusals, never retried with a different workspace.

## 6. API layer

### 6.1 Two defects in `apps/web/src/api/client.ts`

The phase-1 helper has two gaps that phase 2C is the first to hit:

- **It never sends `X-Workspace-Id`.** Phase 1 only called Workspace and
  identity routes, which do not need it. Every route this slice adds does.
- **Its CSRF method list is `["POST", "PATCH", "DELETE"]` and omits `PUT`.**
  The draft route is `PUT /api/v1/agents/{id}/draft` and the backend requires
  CSRF on it. Saving a draft would fail with `csrf_failed` on the first attempt.

The second one is worth naming as a class: an allowlist of write methods drifts
the moment a route uses a method nobody listed. The fix is to invert it — every
method that is not `GET` or `HEAD` carries the CSRF header. That matches the
backend, where CSRF is required by the write dependency rather than by method.

### 6.2 What `api()` becomes

`api(path, init)` gains one option, `workspace`, and gains nothing else. It does
not read a global, a context, or a store:

```ts
await api<AgentResponse[]>("/api/v1/agents", { workspace: workspaceId });
```

An implicit ambient workspace would make a wrong-scope request indistinguishable
from a right one at the call site. An explicit argument makes every scoped call
visibly scoped, and the single derivation of §5 supplies it.

`ApiError` gains a `context` field. Phase 2B puts real data there —
`earliest_available_sequence` and `run_url` on `410` — and the current class
drops it on the floor. Without it, §9.3 cannot resynchronize.

### 6.3 Error codes the console must name

Every code below gets a specific zh-CN message. Anything else falls back to the
server's `detail`, and only a missing `detail` reaches the generic string.

| Code | Where | What the console does |
| --- | --- | --- |
| `agent_alias_taken` | create agent | field-level error on 别名, form stays open |
| `invalid_agent_alias`, `invalid_agent_name` | create agent | field-level error |
| `invalid_agent_spec` | save draft | form-level error, edits preserved |
| `draft_revision_conflict` | save draft | §10.1 |
| `agent_not_published` | create session | tells the user to publish first |
| `state_version_conflict` | run control | §10.2 |
| `event_cursor_too_old` | event stream | §9.3 |
| `idempotency_key_reused` | submit run | surfaced verbatim; the console never silently retries a submission |
| `forbidden`, `workspace_scope_mismatch` | any scoped route | refusal page, no workspace fallback |
| `csrf_failed`, `unauthenticated` | any write | return to `/login` |

## 7. Agent pages

### 7.1 Agent list — `/workspaces/:workspaceId/agents`

Reads `GET /api/v1/agents`. Columns: 名称, 别名, 状态, 当前版本, 创建时间.

Product design §20.4 requires 草稿/已发布/已停用 to be clearly distinguished, so
the status badge is a first-class column and `current_version_id === null`
renders as 尚未发布 rather than an empty cell.

Creation is a dialog with 名称 and 别名 only, matching `CreateAgentRequest`.

### 7.2 Agent detail and draft editor — `/workspaces/:workspaceId/agents/:agentId`

Reads `GET /agents/{id}`, `GET /agents/{id}/draft`, `GET /agents/{id}/versions`.

**Decision: the draft is edited through typed form fields, not a JSON editor.**
Monaco was on the shortlist and is deliberately deferred to phase 4. A free-form
editor invites users to write specs the server will reject with
`invalid_agent_spec`, trading a solved validation problem for an unsolved one,
and it costs a large dependency for a spec shape that is currently five numbers,
one string, and one enum.

The form maps one-to-one onto the spec the deterministic provider accepts:

| Section | Field | Binding |
| --- | --- | --- |
| 基本信息 | 名称, 别名 | read-only here; changed nowhere in 2C |
| 人格 | personality | textarea |
| 模型 | provider | fixed to `deterministic`, disabled, labelled as phase-3 |
| 模型 | scenario | select over `complete` / `continue_once` / `fail_replay_safe` |
| 工具 | — | a stated phase-3 gap; renders no control and writes `tools: []` |
| 安全阀 | max_execution_seconds, max_elapsed_seconds, max_model_calls, max_tool_calls, max_derived_retries | number inputs |

`schema_version` is written as `1` and is not user-editable.

The scenario select is the honest form of the deterministic substitute: the
server validates it with a `Literal`, so the three options are the three the
platform actually has. It is labelled as a substitute, not as a model choice.

Saving sends `PUT /agents/{id}/draft` with `expected_revision` taken from the
loaded draft. Conflict behavior is §10.1.

**Known limit, stated in the UI:** the console cannot tell whether the saved
draft is identical to the published version, because `AgentDraftResponse`
carries no content hash and `AgentVersionResponse` carries no draft revision.
The page shows 草稿修订 and 当前版本 as two separate facts and claims no
relationship between them. Phase 4 can add the comparison when the Builder needs
it; inventing a client-side hash would be a guess about server normalization.

### 7.3 Publish

`POST /agents/{id}/publish` with `expected_revision`, behind an explicit
confirmation dialog. §20.4 requires confirmation for dangerous operations, and
publishing writes an immutable version other Runs will bind to.

The dialog states the draft revision being published. On success the new
version number and `content_hash` are shown, because the hash is the only
evidence the user has that the version is what they saved.

## 8. Run pages

### 8.1 Run list — `/workspaces/:workspaceId/runs`

Reads `GET /api/v1/runs`, optionally with `session_id`. Columns: Run, 状态,
队列, 会话序号, 创建时间, 结束时间.

队列 renders `queue.status` and, when the Run is not head, `queue.position`.
This is the visible half of the phase-2A promise that a Run submitted into a
blocked Session is accepted and explained rather than rejected.

**Known limit:** `GET /api/v1/runs` returns an unbounded list with no pagination
or filtering. The page therefore offers neither. It does not fake a pager over a
list it received whole, and it does not filter client-side and call it a search.
The limit is written in `docs/development.md` so an operator knows the list
degrades at scale.

Submitting a Run needs a Session. The console creates a Session per submission
(`POST /api/v1/sessions`, default `persistent`) and immediately submits the Run
with a client-generated `Idempotency-Key`, then navigates to the Run. Session
management as a first-class concept is phase 4; what phase 2C needs is a
correct, idempotent submission path.

### 8.2 Run detail — `/workspaces/:workspaceId/runs/:runId`

Two panes, and only two:

**概要** — status, `state_version`, `session_sequence`, `blocked_by_run_id`,
`pause_reason`, `retry_of_run_id`, `budget_root_run_id`, `checkpoint_replay_safe`,
`checkpoint_effect_status`, and the `budget` document rendered as
consumed-against-limit rows.

`budget_root_run_id` and `retry_of_run_id` are shown as links, because a
retry sharing its root's budget is the phase-2A rule most likely to surprise
someone, and the link is how they discover it.

**时间线** — the event stream of §9, newest last, each row showing sequence,
`event_type`, `occurred_at`, and the payload.

Panes from §20.3 with no phase-two data — 父子任务树, 上下文和压缩事件, 产物,
Token 和费用 — are **not rendered at all**. An empty pane labelled "暂无产物"
would claim the platform has artifacts and this Run produced none. It has none.

### 8.3 Controls

Buttons are rendered from `available_actions` and from nothing else. The console
does not compute reachable transitions from `status`; the state machine already
decided, and a second opinion in TypeScript is a second source of truth that
will drift.

| Action | Request | Confirmation |
| --- | --- | --- |
| 暂停 | `POST /runs/{id}/pause` with `expected_state_version` | none |
| 继续 | `POST /runs/{id}/resume` with `expected_state_version` | none |
| 取消 | `POST /runs/{id}/cancel` with `expected_state_version` | required |
| 重试 | `POST /runs/{id}/retry` with `Idempotency-Key` | required |

取消 is confirmed because it is irreversible. 重试 is confirmed because it
consumes the shared root budget and counts against `max_derived_retries` —
clicking it three times by accident exhausts the allowance.

Pause is shown as a request, not an act. The button's success message says the
Run will pause at the next safe checkpoint, because that is what phase 2B
implemented and a UI that says 已暂停 while `status` is still `running` teaches
the user something false about the platform.

## 9. The event stream in the browser

### 9.1 `fetch` streaming, not `EventSource`

**Decision: the console reads the stream with `fetch` and a `ReadableStream`
reader.**

`EventSource` is the obvious choice and it cannot do the job. On a non-200
response it fires `error` and closes without exposing the status or the body —
so a client built on it can never read `earliest_available_sequence` out of the
`410`, and §2 item 8 becomes unimplementable. Phase 2B built that hint
deliberately; a console that cannot consume it wastes the feature and shows
users a silently truncated timeline.

`fetch` also lets the console send `X-Workspace-Id` and the cursor as real
headers instead of using the query-parameter convention break, and it puts
reconnection backoff under our control.

The cost is that reconnection is ours to write: on stream end without a terminal
status, reconnect with `Last-Event-Cursor` set to the highest sequence seen,
backing off 1s, 2s, 4s, capped at 10s, and stopping on `403`/`404`.

The query-parameter form is not thereby dead. It exists for third-party
`EventSource` clients, and §12.4 keeps a test on it so the convention break
stays exercised by something.

### 9.2 Framing

Frames are `id: N\nevent: T\ndata: {json}\n\n`. The reader buffers across chunk
boundaries and only parses on a complete `\n\n`; a frame split across two TCP
reads is the ordinary case, not the exceptional one, and a naive
split-per-chunk reader corrupts payloads under exactly the load that matters.

The stream ends by itself when the Run reaches a terminal state. The console
detects this from the terminal event, not from the socket closing, and stops
reconnecting.

### 9.3 `410` resynchronization

On `410 event_cursor_too_old` the console:

1. reads `context.earliest_available_sequence`;
2. refetches the Run snapshot;
3. **renders a gap marker in the timeline** naming how many earlier events are
   no longer retrievable;
4. resubscribes from `earliest_available_sequence - 1`.

Step 3 is the point. Resynchronizing silently produces a timeline that looks
complete and is not, which is the same failure mode as fake data with better
manners.

### 9.4 Live state

Each received event triggers a Run snapshot refetch, so 概要 and
`available_actions` stay consistent with 时间线. TanStack Query owns the
snapshot; the stream owns the timeline. The console never derives run state from
an event type — the state machine is authoritative and the snapshot is how it
speaks.

## 10. Conflict, error, and confirmation behavior

### 10.1 Draft conflicts never auto-merge

`draft_revision_conflict` means someone else saved between the load and the
save. The console:

- keeps the user's edits in the form;
- shows a blocking notice that the draft changed elsewhere;
- offers 重新载入草稿, which discards local edits, and says so in the button's
  confirmation.

It never refetches and retries with the new revision. That is a lost-update with
extra steps: the user would believe they saved their text over a version they
never saw.

### 10.2 Control conflicts do auto-refresh

`state_version_conflict` on pause, resume, or cancel means the Run moved — which
in a system with a Worker is normal, not a mistake. The console refetches the
snapshot, re-renders `available_actions`, and tells the user the Run moved. No
local work is at risk, so there is nothing to protect.

The distinction is deliberate and worth stating once: conflicts that can destroy
typed input are handed to the user; conflicts that only invalidate a button are
resolved automatically.

### 10.3 Theme and accessibility

§20.4 requires 浅色/深色/键盘/无障碍. In scope for 2C:

- Ant Design `darkAlgorithm` selected from `prefers-color-scheme`, with no
  manual toggle (a persisted preference is phase 4);
- every control reachable and operable by keyboard, and every form control with
  a real label — enforced structurally by writing all browser tests with
  `getByRole` and `getByLabel`, which fail on unlabelled controls;
- dialogs that trap focus and close on `Escape`, which Ant Design's `Modal`
  provides and the tests assert.

Not in scope: a screen-reader audit, contrast measurement, or a WCAG claim.
Phase 4 owns those. The console will not claim conformance it has not measured.

## 11. Restart drill

The phase-two exit check `Worker 或 Redis 重启不会丢失已提交状态` has never been
executed. Phase 2B proved two adjacent things — an abandoned lease is recovered,
and an unreachable Redis costs only latency — and neither is a restart.

A checked-in script drives the deployed Compose stack and its transcript goes
into the verification record:

**Worker restart.** Submit a `continue_once` Run, so there is a slice boundary
to interrupt. `docker compose restart worker` while it is `running`. Assert: the
Run reaches `completed`; event sequences are contiguous with no duplicates; the
`run_lease_acquired` count shows the second lease; no committed event is absent.

**Redis restart.** Submit a Run, `docker compose restart redis` immediately,
assert the Run still completes on the polling fallback, then submit a second Run
after Redis is healthy and assert wake-up latency returns to normal.

**Scheduler restart.** Kill the Worker mid-slice without restarting it, restart
the Scheduler, and assert the Scheduler still returns the abandoned Run to
`queued` — the recovery path phase 2B tested in-process, now across a process
boundary.

Two constraints hold throughout, from the project's standing rules:

- **`docker compose restart` only.** No `down -v`, no volume removal. The drill
  is meaningless if it starts by deleting the state it claims to preserve, and
  the phase-2B verification already recorded one confusing session caused by an
  emptied volume.
- The transcript records IDs, statuses, sequences, and timings. No cookies, no
  bootstrap token, no request bodies carrying personality text, no database URL.

This drill is **not** in CI. CI has GitHub service containers, not a Compose
stack, and inventing one there would test a different topology than the one
operators run. It is a documented, repeatable, recorded drill — the same
standing the phase-2B Compose smoke has. The verification record says so plainly
rather than implying CI covers it.

## 12. Verification strategy

### 12.1 Backend

One integration test for §4 against PostgreSQL, plus the retained memory-adapter
unit test. No other backend test changes; the full phase-1, 2A, and 2B suites
must pass untouched.

### 12.2 Component tests (vitest)

The phase-1 tests drive `vi.spyOn(globalThis, "fetch")` with an ordered chain of
`mockResolvedValueOnce`. That worked for four calls. Phase 2C touches roughly
fifteen endpoints plus a streaming response, and an ordered chain would encode
request order as an assertion nobody intended — a reordered `useQuery` would
break tests that are not about ordering.

**Decision: add `msw` as a dev dependency.** Handlers are keyed by method and
path, so tests assert behavior instead of call order; handlers receive real
`Request` objects, so `X-Workspace-Id` and `X-CSRF-Token` are asserted as they
go over the wire rather than as arguments to a spy; and responses can carry real
`ReadableStream` bodies, which the SSE reader needs.

Coverage:

- the CSRF header is present on `PUT` (the §6.1 defect, pinned);
- `X-Workspace-Id` matches the route parameter on every scoped call;
- `draft_revision_conflict` preserves form input and does not resave;
- `state_version_conflict` refetches and re-renders actions;
- buttons appear exactly for the `available_actions` returned;
- the frame reader reassembles a frame split across chunk boundaries;
- a dropped stream reconnects with `Last-Event-Cursor` at the highest sequence
  seen, and no event is rendered twice;
- `410` renders a gap marker and resubscribes from the earliest available
  sequence;
- panes with no phase-two data are absent from the DOM — asserted, because
  "we didn't build it" and "we built an empty one" are indistinguishable to a
  user and must not be to a test.

### 12.3 Browser acceptance (Playwright)

Runs against the Compose stack, so the Worker and Scheduler are real and the
Run in the test genuinely executes.

Bootstrap succeeds once per fresh stack, which today makes spec ordering load
bearing: `foundation.spec.ts` asserts `201` from `POST /bootstrap` and a second
spec doing the same would get `bootstrap_closed`. Rather than add conditionals
to tests, a Playwright **setup project** performs the bootstrap, asserts its
`201`, and saves `storageState`; both specs consume that state.
`foundation.spec.ts` loses its own bootstrap call and keeps every other
assertion — the `201` assertion moves, it does not disappear.

The console spec walks §2 items 1 through 10 in one pass: create agent, edit
draft, publish, submit, watch the timeline fill live, control the Run, and
finally request another Workspace's Run and assert the refusal.

### 12.4 The query-parameter stream form

One Playwright API-level check subscribes to
`GET /runs/{id}/events?workspace_id=…` with the session cookie and no headers,
confirming the `EventSource`-shaped contract still works even though the console
no longer uses it. Without this, §9.1's convention break would have no consumer
and no test.

### 12.5 Static checks

`ruff`, `pyright`, `eslint --max-warnings 0`, `tsc -b`, `vite build`, the full
pytest suite, and the degraded-Redis pytest run — all unchanged and all green.

## 13. Exit criteria and next seams

Phase 2C is ready to merge only when:

- every phase-1, 2A, and 2B check still passes unchanged;
- a duplicate alias returns `409 agent_alias_taken` against PostgreSQL, proven
  by an integration test;
- a browser session completes draft → publish → submit → live timeline →
  control against the Compose stack, with no test-only route and no seeded data;
- a foreign Workspace's Run URL is refused by the server, not hidden by the UI;
- the stream reconnects with no gap and no duplicate, and a `410` produces a
  visible gap marker rather than a quietly shortened timeline;
- the recorded restart drill shows Worker, Redis, and Scheduler restarts losing
  no committed state, with no volume deleted;
- no pane, badge, or placeholder in the console implies data the platform does
  not yet produce;
- `docs/development.md` documents the console, the Run list's missing
  pagination, and how to run the drill.

With those, **phase two's exit checks are all satisfied** and the phase-two
slice of the roadmap closes.

Phase 3 replaces `DeterministicModelProvider` behind the same port and adds
tools and the sandbox. The console seams it will need are already placed: the
model section becomes a real provider choice, the tools section stops being a
stated gap and starts binding tools, and 产物 becomes a pane once Artifacts
exist. Phase 4 turns this minimum console into the full Agent Builder,
Playground, and Run Detail of product design §20, and owns the accessibility and
i18n claims this slice deliberately declines to make.
