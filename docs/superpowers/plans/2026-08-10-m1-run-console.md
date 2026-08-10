# M1 Run Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver phase 2C: the smallest browser console that drives the whole phase-two chain — draft an Agent, publish it, submit a Run, watch its events live, control it — plus the restart drill that closes the last unproven phase-two exit check.

**Architecture:** No new backend route and no new backend behavior beyond the alias fix already landed. The console is a React 19 / Vite 8 / Ant Design 6 single-page app that consumes the committed HTTP surface. The Workspace lives in the URL and the `X-Workspace-Id` header is derived from it in one place. The event stream is read with `fetch` and a `ReadableStream` reader, not `EventSource`, so the `410` resynchronization hint is actually consumable. The server is the only authority on scope, and `available_actions` is the only authority on which buttons exist.

**Tech Stack:** TypeScript, React 19, Vite 8, Ant Design 6, TanStack Query 5, react-router-dom 7, vitest 4, msw, Playwright, Docker Compose

---

## 1. Fixed scope and working rules

- Work only in `.worktrees/m1-run-execution` on branch `m1-run-execution`.
- Treat the product design v2.4 §20 and §27.1, the M1 technical design v1.1, and
  `docs/superpowers/specs/2026-08-10-m1-run-console-design.md` as authoritative.
- Consume the phase-2A and phase-2B HTTP surface exactly as committed. **Add no
  backend route and change no response shape.** The alias fix in `cda07e0` was
  the slice's only backend change and is already done.
- Use `corepack pnpm --filter @tiny-hermes/web <script>` for Node commands. The
  root `pnpm web:*` scripts shell out to a bare `pnpm` that is not on PATH in
  Git Bash; do not rely on them locally.
- Use `uv run --no-sync` for the backend checks.
- Write and observe a failing test before each production behavior.
- **Render nothing the platform cannot produce.** No empty pane, badge, count,
  or placeholder standing in for phase-3 or phase-4 data. When a test asserts a
  pane is absent, it asserts absence from the DOM, not emptiness.
- **Never filter for safety.** The console sends the header the URL implies and
  renders the server's refusal. No client-side membership check, no fallback to
  another Workspace on `403`.
- Every database integration command targets only `tiny_hermes_test`.
- The restart drill uses `docker compose restart` only. No `down -v`, no volume
  removal, in the script or by hand while it runs.
- The verification record copies no cookie, bootstrap token, password, request
  body carrying personality text, or database URL.
- Commit after each task only when its focused checks pass.

## 2. File map

```text
apps/web/src/
├─ api/client.ts                         # workspace option, CSRF by exclusion, ApiError.context
├─ api/types.ts                          # new: response types mirroring the committed shapes
├─ workspace/useWorkspaceId.ts           # new: the one derivation from route param to header
├─ layout/ConsoleLayout.tsx              # new: workspace-scoped shell and navigation
├─ pages/AgentsPage.tsx                  # new: list and create
├─ pages/AgentDetailPage.tsx             # new: draft form, save, publish
├─ pages/RunsPage.tsx                    # new: list and submit
├─ pages/RunDetailPage.tsx               # new: summary, controls, timeline
├─ runs/eventFrames.ts                   # new: pure SSE frame parser
├─ runs/useRunEvents.ts                  # new: streaming reader, reconnect, 410
├─ i18n/zh-CN.ts                         # extended
├─ i18n/en-US.ts                         # extended
├─ App.tsx                               # workspace-scoped routes, dark algorithm
└─ test/
   ├─ setup.ts                           # extended: msw lifecycle
   └─ server.ts                          # new: msw server and default handlers

apps/web/src/**/*.test.ts(x)             # client, eventFrames, useRunEvents, four pages
tests/e2e/
├─ playwright.config.ts                  # setup project, storageState
├─ bootstrap.setup.ts                    # new: one bootstrap per fresh stack
├─ foundation.spec.ts                    # modified: consumes the setup state
├─ console.spec.ts                       # new: the phase-2C acceptance walk
└─ stream-contract.spec.ts               # new: the EventSource-shaped stream form

scripts/restart_drill.py                 # new: Worker, Redis, Scheduler restarts
.github/workflows/ci.yml                 # compose-e2e gains the drill step
docs/development.md                      # console, Run list limits, how to run the drill
docs/superpowers/verification/2026-08-10-m1-run-console.md
```

---

### Task 0: The alias fix — already done

Landed in `cda07e0` before this plan: the SQL Agent Catalog translates
`uq_agents_workspace_alias` into `AgentAliasAlreadyUsed`, two PostgreSQL
integration tests cover the refusal and the per-Workspace scope, and the full
suite is at 271 passing. Nothing to do here; listed so the task numbering
matches the design document's §4.

---

### Task 1: The API layer

**Files:**
- Modify: `apps/web/src/api/client.ts`
- Create: `apps/web/src/api/types.ts`
- Create: `apps/web/src/workspace/useWorkspaceId.ts`
- Create: `apps/web/src/test/server.ts`
- Modify: `apps/web/src/test/setup.ts`
- Modify: `apps/web/package.json`
- Create: `apps/web/src/api/client.test.ts`

- [ ] **Step 1: Install msw and stand up the test server**

Add `msw` to `devDependencies` and regenerate the lockfile. Create
`test/server.ts` exporting `setupServer()` with no default handlers, and wire
`listen({ onUnhandledRequest: "error" })` / `resetHandlers` / `close` into
`test/setup.ts`.

`onUnhandledRequest: "error"` is deliberate: a page that fires a request no test
declared should fail loudly, not silently receive `undefined`.

Rewrite `App.test.tsx` onto msw handlers in the same step. It currently chains
`mockResolvedValueOnce`, which encodes call order as an assertion; leaving one
file on the old style guarantees the next person copies it.

- [ ] **Step 2: Write the failing client tests**

Against msw handlers that echo the received request:

- a `GET` carries no `X-CSRF-Token`;
- a `POST`, a `PATCH`, a `DELETE`, **and a `PUT`** each carry it when the CSRF
  cookie is present — `PUT` is the regression this task exists for;
- a request with `{ workspace }` carries `X-Workspace-Id` with that exact value,
  and one without the option carries no such header at all;
- a `problem+json` body with `context` produces an `ApiError` whose `context`
  survives — assert `earliest_available_sequence` specifically, since §9.3
  cannot work without it;
- a `204` resolves to `undefined`;
- a network failure produces `ApiError(0, "network_failed")`.

- [ ] **Step 3: Verify red, then implement**

The CSRF fix inverts the rule rather than extending the list:

```ts
if (method !== "GET" && method !== "HEAD" && csrf !== undefined) {
  headers.set("X-CSRF-Token", csrf);
}
```

An allowlist of write methods drifts the moment a route uses a method nobody
enumerated, which is exactly how `PUT` was missed. Leave a short comment saying
so, and naming that the backend gates CSRF on the write dependency rather than
on the method.

Add `workspace?: string` to the init type and set `X-Workspace-Id` from it. Do
not read a context, store, or module-level variable — an ambient workspace makes
a wrong-scope call indistinguishable from a right one at the call site.

Add `readonly context: Record<string, unknown>` to `ApiError`.

- [ ] **Step 4: Add the response types and the workspace hook**

`api/types.ts` mirrors the committed response shapes by hand: `AgentResponse`,
`AgentDraftResponse`, `AgentVersionResponse`, `SessionResponse`, `RunResponse`
with its nested `queue` and `budget`, and `RunEventFrame`. Field names match the
server's snake_case exactly; no renaming layer, because a rename is a place for
a field to quietly disappear.

`useWorkspaceId()` reads the route parameter, validates it is a UUID, and
returns it. This is the single derivation named in design §5.

- [ ] **Step 5: Verify green and commit**

```bash
corepack pnpm --filter @tiny-hermes/web test && corepack pnpm --filter @tiny-hermes/web lint
```

```bash
git commit -m "feat: scope api requests to a workspace and fix csrf on put"
```

---

### Task 2: The console shell and workspace-scoped routes

**Files:**
- Modify: `apps/web/src/App.tsx`
- Create: `apps/web/src/layout/ConsoleLayout.tsx`
- Modify: `apps/web/src/pages/WorkspacesPage.tsx`
- Modify: `apps/web/src/i18n/zh-CN.ts`, `apps/web/src/i18n/en-US.ts`
- Create: `apps/web/src/layout/ConsoleLayout.test.tsx`

- [ ] **Step 1: Write the failing shell tests**

- rendering `/workspaces/:id/agents` shows navigation to both Agents and Runs
  with the workspace ID preserved in both links;
- a route parameter that is not a UUID renders a refusal, and fires **no**
  API request (msw's `onUnhandledRequest: "error"` will catch a stray one);
- the layout renders the signed-in user and a working 退出;
- `prefers-color-scheme: dark` selects Ant Design's dark algorithm — assert
  through the `ConfigProvider` theme rather than by sampling a pixel.

- [ ] **Step 2: Implement**

Add the routes from design §5 and a `ConsoleLayout` holding navigation, the
current Workspace name, and the user menu. `WorkspacesPage` entries become links
into `/workspaces/:id/agents`.

Ant Design's algorithm is selected from `window.matchMedia`. No manual toggle
and no persisted preference — those are phase 4.

The existing `test/setup.ts` stubs `matchMedia` to always return
`matches: false`; extend the stub to take the query into account so the dark
test can assert something real instead of the stub's constant.

- [ ] **Step 3: Verify and commit**

---

### Task 3: Agent list and creation

**Files:**
- Create: `apps/web/src/pages/AgentsPage.tsx`
- Modify: `apps/web/src/App.tsx`, both i18n files
- Create: `apps/web/src/pages/AgentsPage.test.tsx`

- [ ] **Step 1: Write the failing list tests**

- the list request carries `X-Workspace-Id` equal to the route parameter;
- an Agent with `current_version_id: null` renders 尚未发布, and one with a
  version renders 已发布 — §20.4 requires draft and published to be clearly
  distinguished, so this is asserted, not incidental;
- creating an Agent posts `{name, alias}` and shows the new row;
- a `409 agent_alias_taken` shows a field-level error on 别名, **keeps the
  dialog open, and keeps the typed values** — the whole point of the backend fix
  is that this is now recoverable without retyping;
- a `403 forbidden` renders a refusal and does not redirect to another
  Workspace.

- [ ] **Step 2: Implement**

Follow `WorkspacesPage`'s existing shape: TanStack Query for the list, a
mutation for creation, an Ant Design `Modal` with a `Form`. Every field carries
a real label so `getByLabel` works — that is the accessibility floor design
§10.3 commits to.

- [ ] **Step 3: Verify and commit**

---

### Task 4: The draft editor

**Files:**
- Create: `apps/web/src/pages/AgentDetailPage.tsx`
- Modify: `apps/web/src/App.tsx`, both i18n files
- Create: `apps/web/src/pages/AgentDetailPage.test.tsx`

- [ ] **Step 1: Write the failing editor tests**

- the loaded draft populates 人格, the scenario select, and all five safety-valve
  numbers;
- the scenario select offers exactly `complete`, `continue_once`, and
  `fail_replay_safe` — the three the server's `Literal` accepts, so an added
  fourth option would be a lie about the platform;
- saving sends `PUT` to `/api/v1/agents/{id}/draft` with the loaded
  `expected_revision`, a `schema_version` of `1`, `tools: []`, and the CSRF
  header (the Task 1 fix, pinned again at the call site that needs it);
- `409 draft_revision_conflict` shows the conflict notice, **leaves the typed
  personality in the form**, and issues no follow-up `PUT`;
- 重新载入草稿 asks for confirmation, then refetches and replaces the form;
- `422 invalid_agent_spec` shows a form-level error and preserves input;
- the tools section renders as a stated phase-3 gap with no control that binds
  anything.

- [ ] **Step 2: Implement**

Typed form fields, not a JSON editor — design §7.2 states why, and Monaco is not
a dependency of this slice.

The save mutation's `onError` must not refetch-and-retry. Write the reason in a
comment: auto-retrying with the server's new revision is a lost update wearing a
success message.

Show 草稿修订 and 当前版本 as two independent facts. Do not compute or imply
whether they represent the same content; the API exposes no draft content hash
(design §7.2's known limit). State the limit in the UI text.

- [ ] **Step 3: Verify and commit**

---

### Task 5: Publish

**Files:**
- Modify: `apps/web/src/pages/AgentDetailPage.tsx`, both i18n files
- Modify: `apps/web/src/pages/AgentDetailPage.test.tsx`

- [ ] **Step 1: Write the failing publish tests**

- 发布 opens a confirmation naming the draft revision, and **no request is sent
  until it is confirmed** — assert the un-sent case, since that is the half a
  confirmation dialog actually protects;
- confirming posts `expected_revision` and renders the returned
  `version_number` and `content_hash`;
- a re-publish returning `200` (unchanged content) is presented as "no new
  version" rather than as a failure, matching the server's two-status contract;
- `409 draft_revision_conflict` behaves as in Task 4;
- the dialog closes on `Escape` and returns focus to the trigger.

- [ ] **Step 2: Implement**

- [ ] **Step 3: Verify and commit**

---

### Task 6: Run list and submission

**Files:**
- Create: `apps/web/src/pages/RunsPage.tsx`
- Modify: `apps/web/src/App.tsx`, both i18n files
- Create: `apps/web/src/pages/RunsPage.test.tsx`

- [ ] **Step 1: Write the failing list tests**

- rows render status, `session_sequence`, and timestamps;
- a Run with `queue.status` other than head renders `queue.position`, and a head
  Run does not render a position — the visible half of phase 2A's promise that a
  Run into a blocked Session is accepted and explained;
- submitting creates a Session, then posts the Run with a **non-empty
  `Idempotency-Key`**, then navigates to the Run;
- `409 agent_not_published` tells the user to publish first;
- `409 idempotency_key_reused` is surfaced, and **no automatic retry is sent** —
  assert the request count, because a console that silently resubmits defeats
  the idempotency record;
- the page renders no pagination control.

- [ ] **Step 2: Implement**

Generate the idempotency key with `crypto.randomUUID()` once per submission
attempt and keep it stable across a retry of the *same* attempt.

Add a visible note that the list is unpaginated, and record the same limit in
`docs/development.md` in Task 10.

- [ ] **Step 3: Verify and commit**

---

### Task 7: The event stream reader

**Files:**
- Create: `apps/web/src/runs/eventFrames.ts`
- Create: `apps/web/src/runs/useRunEvents.ts`
- Create: `apps/web/src/runs/eventFrames.test.ts`
- Create: `apps/web/src/runs/useRunEvents.test.ts`

This task is first among the Run-detail work because it is the only part with
real failure modes, and the pane in Task 8 is a thin renderer over it.

- [ ] **Step 1: Write the failing frame-parser tests**

`eventFrames.ts` holds a pure incremental parser — one function, no network, no
React — in the same spirit as the backend's `slice_policy` and `cursor_is_stale`:

```ts
export function readFrames(buffer: string): { frames: RunEventFrame[]; rest: string }
```

Cases:

- a whole frame parses into `{sequence, event_type, occurred_at, payload}`;
- **a frame split across two chunks** yields nothing on the first call and the
  whole frame once the remainder arrives — this is the ordinary case over a real
  socket, not an edge case;
- two frames in one chunk both parse;
- a trailing partial frame stays in `rest` and is never emitted;
- a heartbeat comment line is skipped without disturbing the buffer.

- [ ] **Step 2: Write the failing stream-hook tests**

msw serves a `ReadableStream` body, so the hook is tested against real streaming
rather than a mocked abstraction:

- events arrive in order and the terminal event stops the reader;
- a stream that ends early reconnects with `Last-Event-Cursor` set to the
  highest sequence seen, and **no event is delivered twice**;
- `410` with `context.earliest_available_sequence` produces a gap marker of the
  right size and resubscribes from `earliest_available_sequence - 1`;
- `403` and `404` stop the reader and do not reconnect;
- unmounting aborts the in-flight request — assert the `AbortSignal` fired, so a
  navigated-away page cannot hold a connection open.

- [ ] **Step 3: Implement**

`fetch` with `AbortController`, `credentials: "include"`, `X-Workspace-Id`, and
`Last-Event-Cursor`. Backoff 1s, 2s, 4s, capped at 10s.

Add a module docstring recording why this is not `EventSource`: on a non-200 it
fires `error` and closes without exposing status or body, so the phase-2B `410`
hint would be unreadable and the timeline would silently truncate.

- [ ] **Step 4: Verify and commit**

---

### Task 8: Run detail

**Files:**
- Create: `apps/web/src/pages/RunDetailPage.tsx`
- Modify: `apps/web/src/App.tsx`, both i18n files
- Create: `apps/web/src/pages/RunDetailPage.test.tsx`

- [ ] **Step 1: Write the failing detail tests**

- 概要 renders status, `state_version`, `checkpoint_replay_safe`, and every
  budget row as consumed-against-limit;
- `retry_of_run_id` and `budget_root_run_id` render as links when present;
- **buttons come from `available_actions` and nothing else**: a snapshot with
  `["pause"]` renders 暂停 and neither 继续 nor 取消, whatever `status` says;
- 暂停 posts `expected_state_version` and the success message says the Run will
  pause at the next safe checkpoint — assert the wording, because "已暂停" while
  `status` is still `running` teaches the user something false;
- 取消 and 重试 require confirmation and send nothing until confirmed;
- `409 state_version_conflict` refetches the snapshot, re-renders the buttons,
  and tells the user the Run moved — the opposite of Task 4's handling, because
  no typed input is at risk;
- a received event triggers a snapshot refetch;
- 时间线 renders the sequence, type, and time of each event, and renders the gap
  marker Task 7 produces;
- **父子任务树, 上下文和压缩事件, 产物, and Token 和费用 are absent from the
  DOM** — asserted by name, because "not built" and "built and empty" look
  identical to a user and must not look identical to the suite.

- [ ] **Step 2: Implement**

TanStack Query owns the snapshot; the Task 7 hook owns the timeline. Never
derive run state from an event type.

- [ ] **Step 3: Verify and commit**

---

### Task 9: Browser acceptance

**Files:**
- Create: `tests/e2e/bootstrap.setup.ts`
- Modify: `tests/e2e/playwright.config.ts`
- Modify: `tests/e2e/foundation.spec.ts`
- Create: `tests/e2e/console.spec.ts`
- Create: `tests/e2e/stream-contract.spec.ts`

- [ ] **Step 1: Add the setup project**

Bootstrap succeeds once per fresh stack, so two specs both calling it cannot
both pass. Add a Playwright `setup` project that posts `/api/v1/bootstrap`,
asserts `201`, logs in, and saves `storageState`. Make the main project depend
on it and consume that state.

Remove the bootstrap call from `foundation.spec.ts` and keep every other
assertion. The `201` assertion moves into the setup project; it does not
disappear.

- [ ] **Step 2: Write the console acceptance walk**

One spec covering design §2 items 1 through 10 against the running stack, so the
Run genuinely executes on the real Worker:

create an Agent → edit the draft with the `continue_once` scenario → publish and
read back the version number → submit a Run → watch the timeline fill and the
status reach `completed` **without reloading the page** → open a
`fail_replay_safe` Run and use 重试 → finally request a Run URL under a second
Workspace the user is not scoped to and assert the server refuses.

Reload once mid-Run and assert the timeline is complete afterwards: that is the
resume path, exercised the way a user actually triggers it.

- [ ] **Step 3: Keep the query-parameter stream form tested**

The console now uses `fetch` with headers, so nothing else exercises the
`workspace_id` query parameter that exists for `EventSource` clients. One
API-level check subscribes with the session cookie and the query parameter only,
and asserts the frames arrive. Without this, design §9.1's convention break
would have no consumer and no test.

- [ ] **Step 4: Verify and commit**

```bash
docker compose -f deploy/compose/compose.yaml up -d --build --wait
corepack pnpm exec playwright test --config tests/e2e/playwright.config.ts
```

---

### Task 10: Restart drill, CI, docs, and the exit record

**Files:**
- Create: `scripts/restart_drill.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/development.md`
- Create: `docs/superpowers/verification/2026-08-10-m1-run-console.md`

- [ ] **Step 1: Write the drill**

Three scenarios against the running stack, per design §11:

1. **Worker restart.** Submit a `continue_once` Run; `docker compose restart
   worker` while it is `running`; assert it reaches `completed`, that event
   sequences are contiguous with no duplicates, and that a second
   `run_lease_acquired` records the new lease.
2. **Redis restart.** Submit a Run and restart Redis immediately; assert it
   still completes on the polling fallback; submit another after Redis is
   healthy and assert wake-up latency returns.
3. **Scheduler restart.** Kill the Worker mid-slice without restarting it,
   restart the Scheduler, and assert the abandoned Run returns to `queued` —
   the recovery phase 2B tested in-process, now across a process boundary.

`docker compose restart` only. The script must not contain `down`, `-v`, or
`rm`; a drill that begins by deleting the state it claims to preserve proves
nothing, and phase 2B already lost an hour to an emptied volume.

Print IDs, statuses, sequences, and timings. Print no cookie, token, or request
body.

- [ ] **Step 2: Add the drill to CI**

`compose-e2e` already builds and starts the real stack, so the drill runs there
as a step **after** the Playwright run — a Worker restarting underneath the
browser tests would make their failures unreadable.

An exit check proven once by a transcript decays the next time lease handling
changes; the point of this check is that recovery keeps working.

- [ ] **Step 3: Update development documentation**

Add a console section: the routes and what each page is for, that the Run list
is unpaginated and why, that the console reads the stream with `fetch` while the
`workspace_id` query parameter remains for `EventSource` clients, and how to run
the drill. State plainly which §20.3 panes do not exist yet.

- [ ] **Step 4: Run the complete fresh verification**

```bash
uv run --no-sync ruff check packages/backend
uv run --no-sync pyright
uv run --no-sync pytest packages/backend/tests -q
corepack pnpm --filter @tiny-hermes/web lint
corepack pnpm --filter @tiny-hermes/web test
corepack pnpm --filter @tiny-hermes/web build
docker compose -f deploy/compose/compose.yaml up -d --build --wait
corepack pnpm exec playwright test --config tests/e2e/playwright.config.ts
uv run --no-sync python scripts/restart_drill.py
```

Do not run `ruff format`; it is not one of this repository's checks and would
reformat most of the backend.

- [ ] **Step 5: Write the verification record**

Follow the phase-2B record's structure: commits, environment, commands with real
results, what the slice proves, deliberate deviations, known limits, redacted
failure evidence, and the §3 checklist below.

State two things plainly rather than by implication:

- **CI has still never run.** `git remote -v` is empty. Every CI claim rests on
  the steps having been reproduced locally.
- Which acceptance scenarios are covered by an automated check and which by the
  recorded drill.

- [ ] **Step 6: Commit the verified slice**

---

## 3. Phase 2C completion checklist

- [ ] A duplicate alias returns `409 agent_alias_taken` against PostgreSQL.
- [ ] Every scoped request carries `X-Workspace-Id` derived from the route
      parameter, and no page holds an ambient workspace.
- [ ] `PUT` carries `X-CSRF-Token`, pinned by a test.
- [ ] A browser session completes draft → publish → submit → live timeline →
      control against the Compose stack, with no test-only route and no seeded
      data.
- [ ] A foreign Workspace's Run URL is refused by the server, not hidden by the
      console.
- [ ] The stream resumes after a drop with no gap and no duplicate.
- [ ] A `410` produces a visible gap marker sized from
      `earliest_available_sequence`, never a silently shortened timeline.
- [ ] Run controls are rendered from `available_actions` alone.
- [ ] A draft conflict preserves typed input and sends no automatic retry; a
      control conflict refetches and re-renders.
- [ ] 取消, 重试, and 发布 send nothing before an explicit confirmation.
- [ ] 父子任务树, 上下文和压缩事件, 产物, and Token 和费用 are absent from the
      DOM, asserted by name.
- [ ] The `workspace_id` query-parameter stream form still has a test.
- [ ] Worker, Redis, and Scheduler restarts lose no committed state, in a
      recorded run and in CI, with no volume deleted.
- [ ] `docs/development.md` documents the console, the Run list's missing
      pagination, and how to run the drill.
- [ ] All existing phase-1, 2A, and 2B checks pass unchanged.
