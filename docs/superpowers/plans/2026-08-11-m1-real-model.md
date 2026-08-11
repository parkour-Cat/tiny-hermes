# M1 Real Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver phase 3A: an Agent talks to a real OpenAI-compatible model endpoint, through a client that treats every outbound address as hostile until it is proven otherwise, and a Run's rounds finally accumulate into the Session transcript.

**Architecture:** Two new backend modules. `outbound/` is the only place in the process allowed to construct an HTTP client or a socket, enforced by lint rather than by review. `model_catalog/` is the platform-scoped registry of approved endpoints. The existing `ModelProvider` port keeps its shape and gains a second implementation; the deterministic provider stays exactly as it is. The one change that reaches into phase-2B code is the transcript: a completed round now appends an assistant message in the same transaction that records the slice.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2 async, PostgreSQL 17, Alembic, httpx, ruff, pyright, pytest — plus a small React change in the existing console.

---

## 1. Fixed scope and working rules

- Work in `.worktrees/m1-run-execution` on branch `m1-real-model`.
- Treat the product design v2.4, the M1 technical design v1.1 (especially §9,
  §12.3, §14), and
  `docs/superpowers/specs/2026-08-11-m1-real-model-design.md` as authoritative.
- **No test in this repository calls a real vendor endpoint.** CI has no
  credential and must never need one. The real-endpoint check is a documented
  manual step recorded in the verification record.
- **No sandbox, no tools, no streaming, no Secret table.** `AgentSpec.tools`
  stays `tuple[()]`. Do not add a column, a type, or a branch for any of them.
  Design §3 and §14 list every deferral; if a task seems to need one, stop and
  re-read rather than building a small version.
- **Declare no type that has no producer.** The CanonicalMessage block alias has
  one member in this slice. Tool-call and tool-result blocks arrive in 3C.
- **The credential never leaves the outbound call.** Not in a log line, an event
  payload, an audit context, an exception message, or an API response. Assert
  this rather than intending it.
- **A refused outbound address is not shown to a workspace member.** The
  resolved IP goes in the audit event; the run event payload gets the refusal
  code only. An outbound-policy probe is exactly what that field would be used
  for.
- Every database integration command targets only `tiny_hermes_test`.
- Use `uv run --no-sync` for backend commands and
  `corepack pnpm --filter @tiny-hermes/web <script>` for Node.
- Do not run `ruff format`.
- Write and observe a failing test before each production behavior.
- Commit after each task only when its focused checks pass.

## 2. File map

```text
packages/backend/src/tiny_hermes/
├─ outbound/
│  ├─ domain/address_policy.py       # new: pure verdict over resolved addresses
│  ├─ errors.py                      # new: OutboundRefused and friends
│  ├─ resolver.py                    # new: hostname -> vetted addresses
│  ├─ transport.py                   # new: pinning httpx transport
│  └─ client.py                      # new: SafeOutboundClient, redirect loop
├─ model_catalog/
│  ├─ domain/models.py               # new: ModelEndpoint, UsageQuality
│  ├─ ports/store.py                 # new
│  ├─ application/service.py         # new: register, update, check
│  ├─ infrastructure/tables.py       # new
│  ├─ infrastructure/sql_store.py    # new
│  ├─ infrastructure/credentials.py  # new: credential_ref -> value, never stored
│  └─ presentation/routes.py         # new
├─ agents/domain/models.py           # modified: ModelPolicy union
├─ agents/application/catalog.py     # modified: publish-time endpoint validation
├─ runs/ports/model.py               # modified: CanonicalMessage, ModelResponse
├─ runs/ports/store.py               # modified: ExecutionContext carries messages
├─ runs/infrastructure/deterministic_model.py   # modified: same behavior, new port
├─ runs/infrastructure/openai_model.py          # new: the adapter
├─ runs/infrastructure/sql_store.py  # modified: transcript read and append
├─ runs/application/worker.py        # modified: build request from messages
├─ api/app.py, api/cli.py            # modified: wiring, provider registry
└─ shared/config.py                  # modified: outbound and retry settings

migrations/versions/20260811_0004_model_endpoints.py   # new

apps/web/src/pages/AgentDetailPage.tsx  # modified: provider selector
apps/web/src/api/types.ts               # modified: ModelEndpoint
apps/web/src/i18n/{zh-CN,en-US}.ts      # extended
```

---

### Task 1: The address policy

**Files:**
- Create: `packages/backend/src/tiny_hermes/outbound/domain/address_policy.py`
- Create: `packages/backend/tests/unit/outbound/test_address_policy.py`

This is the security control, and it is a pure function over `ipaddress`
objects, so it is settled before any socket exists.

- [ ] **Step 1: Write the failing table-driven test**

One parametrized test over every class in design §7.1, in **both** IPv4 and
IPv6, asserting refusal with a named reason:

| Input | Reason |
|---|---|
| `127.0.0.1`, `::1` | `loopback` |
| `169.254.169.254`, `fe80::1` | `link_local` |
| `100.100.100.200` | `carrier_grade_nat` |
| `10.0.0.5`, `172.16.0.1`, `192.168.1.1`, `fd00::1` | `private` |
| `0.0.0.0`, `::`, `224.0.0.1`, `ff02::1` | `reserved` |
| `93.184.216.34`, `2606:2800::1` | allowed |

Then the approval path: `10.0.0.5` with `10.0.0.0/8` approved is allowed, and
`10.1.0.5` with `10.0.0.0/24` approved is still refused.

Then the case that is the whole reason this is a list and not a value: a
hostname resolving to `[93.184.216.34, 127.0.0.1]` is **refused**. Checking the
first address only is the bug this test exists to prevent.

- [ ] **Step 2: Implement**

```python
def verdict(addresses: Sequence[IPv4Address | IPv6Address],
            approved: Sequence[IPv4Network | IPv6Network]) -> AddressVerdict
```

`AddressVerdict` is allowed-with-chosen-address, or refused-with-reason. Use
`ipaddress`'s own `is_loopback`, `is_link_local`, `is_private`, `is_reserved`,
`is_multicast`, `is_unspecified` rather than hand-written ranges; add
`100.64.0.0/10` explicitly, because Python does not classify carrier-grade NAT
as private and the Alibaba Cloud metadata address lives there.

Order the checks so the reported reason is the most specific one — link-local
before private, so a metadata address is never reported as merely "private".

- [ ] **Step 3: Verify and commit**

---

### Task 2: SafeOutboundClient

**Files:**
- Create: `outbound/errors.py`, `outbound/resolver.py`, `outbound/transport.py`, `outbound/client.py`
- Create: `packages/backend/tests/integration/outbound/test_safe_client.py`
- Create: `packages/backend/tests/integration/outbound/conftest.py`

- [ ] **Step 1: Write the failing client tests**

Against a real `uvicorn` server on `127.0.0.1`, the technique phase 2B used for
SSE and for the same reason: a transport that buffers or short-circuits proves
nothing about a client. The fixture builds the client with `127.0.0.0/8`
approved, which exercises the approval path and leaves default-deny under test
in Task 1.

- a plain `POST` reaches the stand-in and returns its body;
- a request to a host resolving outside the approval raises `OutboundRefused`
  carrying the reason, and **no connection is attempted** — assert the
  stand-in recorded no request;
- a `302` to a forbidden address is refused at that hop, and the stand-in's
  second handler is never reached;
- a `302` across origin arrives at the second server with **no**
  `Authorization` header — assert on what the second server received;
- a `302` to the same origin keeps it;
- six redirects raises rather than looping;
- a response larger than the cap raises rather than buffering it;
- a read timeout raises an error whose `external_effect_unknown` is `True`,
  while a connect failure's is `False`.

Then the rebinding test, driven by a stub resolver because real DNS cannot be
made to lie on demand: a resolver that answers `[permitted]` on the first call
and `[forbidden]` on the second, and an assertion that the connection went to
the permitted address. This is what pinning means and it is invisible in every
other test.

- [ ] **Step 2: Implement**

`resolver.py` wraps `getaddrinfo` in a thread and returns every address.
`transport.py` subclasses `httpx.AsyncHTTPTransport` and rewrites the request's
host to the pinned literal while setting `extensions["sni_hostname"]` and the
`Host` header, so certificate verification still runs against the hostname.
`client.py` holds the loop: resolve, vet, pin, send with
`follow_redirects=False`, inspect, repeat.

Budgets from settings: 5s connect, 60s read, 5 redirects, 10 MiB.

Module docstring records why this is not `httpx` with careful arguments: a
library's redirect follower re-resolves and re-sends without re-vetting, which
is precisely the hop an attacker controls.

- [ ] **Step 3: Verify and commit**

---

### Task 3: The architecture ban

**Files:**
- Modify: `pyproject.toml`
- Create: `packages/backend/tests/unit/outbound/test_client_ban.py`

- [ ] **Step 1: Write the failing ban test**

Two assertions, because a lint rule that is configured and does not bite is
worse than none:

1. Run `ruff check` over a temporary file that constructs `httpx.AsyncClient`,
   with the repository's configuration, and assert it fails with `TID251`.
2. Read `pyproject.toml` and assert the set of paths exempted from `TID251` is
   exactly the outbound module, the test tree, and `scripts/`. Widening the
   exemption then shows up as a failing test, not as a quiet diff.

- [ ] **Step 2: Configure**

```toml
[tool.ruff.lint.flake8-tidy-imports.banned-api]
"httpx.AsyncClient".msg = "Outbound requests go through tiny_hermes.outbound."
"httpx.Client".msg = "..."
"httpx.AsyncHTTPTransport".msg = "..."
"socket.socket".msg = "..."
"urllib.request.urlopen".msg = "..."
```

Add `TID` to `select`, and per-file-ignores for the three exempted paths.
Expect the existing suite to surface a handful of legitimate constructions in
tests; exempt the test tree, not the individual files.

- [ ] **Step 3: Verify the whole repository still lints, and commit**

---

### Task 4: The model endpoint catalog

**Files:**
- Create: `model_catalog/domain/models.py`, `ports/store.py`, `infrastructure/tables.py`, `infrastructure/sql_store.py`, `infrastructure/credentials.py`
- Create: `migrations/versions/20260811_0004_model_endpoints.py`
- Create: `packages/backend/tests/unit/model_catalog/test_endpoint_rules.py`
- Create: `packages/backend/tests/integration/model_catalog/test_endpoint_store.py`

- [ ] **Step 1: Write the failing domain tests**

`ModelEndpoint` validation, with no database: `base_url` must be `https`, or
`http` only when its address falls inside an approved CIDR; `context_window` and
`max_output_tokens` positive; `usage_quality` is `provider` or `unavailable` and
**not** `estimated` — assert the value is rejected, so a later reviewer sees
that its absence is deliberate rather than forgotten; `credential_ref` matches
`^[A-Z][A-Z0-9_]*$`, because it names an environment variable and anything else
is a misunderstanding worth catching early.

- [ ] **Step 2: Write the failing store and migration tests**

Against PostgreSQL: insert, read, list-active, unique name, and the migration's
upgrade/downgrade round trip. The table is platform-scoped — assert there is no
`workspace_id` column, so a later "small change" adding one has to argue with a
test.

- [ ] **Step 3: Implement**

Table per design §6.1. `credentials.py` holds one function,
`resolve_credential(ref) -> str`, reading `os.environ` and raising a named error
when absent. It never caches, never logs the value, and its return type is a
plain `str` that no caller stores — a wrapper class here would be security
theatre, since the value ends up in a header either way.

- [ ] **Step 4: Verify and commit**

---

### Task 5: Endpoint routes and the connectivity check

**Files:**
- Create: `model_catalog/application/service.py`, `model_catalog/presentation/routes.py`
- Modify: `api/app.py`
- Create: `packages/backend/tests/integration/model_catalog/test_endpoint_api.py`

- [ ] **Step 1: Write the failing route tests**

- `POST` as a workspace admin → `403`; as a platform administrator → `201`;
- `POST` with a `base_url` whose host resolves to a forbidden address → `422`
  with the refusal code, and no row written;
- `POST` naming a `credential_ref` that is absent from the process environment →
  `422`, because a broken configuration must be found here and not inside
  someone's Run;
- `GET` as an ordinary workspace member → `200`, and the body contains no
  `credential_ref` and no credential — assert on the absence of the key, not on
  the absence of the value;
- `PATCH` disabling an endpoint → `200`, and it stops appearing in the active
  list;
- `POST /{id}/check` → a verdict object with `reachable` and `elapsed_ms`, and
  for an unreachable endpoint a refusal code — and in neither case any part of
  the endpoint's response body.

- [ ] **Step 2: Implement**

The check sends one minimal completion request through SafeOutboundClient and
classifies the outcome. It reports a verdict and a duration and nothing else; a
`base_url` mistakenly pointing at an internal service would otherwise turn this
route into a read primitive for whoever can call it.

Audit events for every write, per §12.2 item 6.

- [ ] **Step 3: Verify and commit**

---

### Task 6: The transcript

**Files:**
- Modify: `runs/ports/model.py`, `runs/ports/store.py`, `runs/infrastructure/sql_store.py`, `runs/application/worker.py`, `runs/infrastructure/deterministic_model.py`
- Modify: `packages/backend/tests/unit/runs/*` as the port changes require
- Create: `packages/backend/tests/integration/runs/test_transcript.py`

This is the largest task and the one the roadmap does not mention. Design §4
explains why: `session_messages` has existed since phase 2A, Run creation writes
the user's message, and **nothing has ever written an assistant message**.

- [ ] **Step 1: Write the failing transcript tests**

- a completed round appends exactly one `assistant` message to the Session,
  with `source_run_id` set and the sequence allocated by
  `sessions.next_message_sequence`;
- a **failed** round appends none — the transcript holds what the Agent said,
  never what it tried to say;
- a two-round Run appends two, in order;
- a second Run in the same persistent Session builds its request from the first
  Run's messages: assert the `ModelRequest` the provider received, through a
  recording provider, contains the earlier exchange;
- an `ephemeral` Session's second Run does not — assert the mode is honoured
  rather than assumed;
- the append and the slice record commit together: with the slice record forced
  to fail, no assistant message is left behind.

- [ ] **Step 2: Rewrite the port**

`ModelRequest` carries `messages: tuple[CanonicalMessage, ...]` instead of
`input_text`. `ModelResponse` replaces `tokens: int = 0` with
`input_tokens: int | None`, `output_tokens: int | None`, and `usage_quality`.
Zero is a number the platform would then have to tell apart from "not
reported", and it has been getting away with conflating them only because the
stand-in always reports.

`Block = TextBlock` as an alias, not a one-member union. Record in the module
docstring that 3C widens it and that every `match` will fail typechecking then,
which is the review the widening deserves.

- [ ] **Step 3: Implement**

`ExecutionContext.input_text` becomes `messages`, read in the same query that
already reads the Run's user message. The append joins the existing atomic
slice record in `sql_store.py` — the same transaction, not a second one.

The deterministic provider keeps its behavior exactly: it still branches on
`round_index` and returns the same three scenarios with the same text. Only its
signature moves. Every phase-2B and 2C test must pass untouched, and if one
needs editing, that is a signal the behavior moved.

- [ ] **Step 4: Verify the full suite, and commit**

---

### Task 7: The OpenAI-compatible adapter

**Files:**
- Create: `runs/infrastructure/openai_model.py`
- Create: `packages/backend/tests/unit/runs/test_openai_normalization.py`
- Create: `packages/backend/tests/integration/runs/test_openai_provider.py`

- [ ] **Step 1: Write the failing normalization tests**

Pure, over recorded response bodies, no network:

| Body | Result |
|---|---|
| `finish_reason: "stop"` with content | `completed`, text, usage |
| `finish_reason: "length"` | `failed`, `max_output_reached` |
| `finish_reason: "tool_calls"` | `failed`, `tool_use_not_supported` |
| unrecognized `finish_reason` | `failed`, `unsupported_stop_reason` |
| `"stop"` with empty content | `failed`, `empty_response` |
| no `usage` object | `usage_quality=unavailable`, both counts `None` |
| `usage` present | `usage_quality=provider`, both counts set |

Every one of those failures is `replay_safe=True`: nothing was written anywhere.
Assert that too, because it is what decides whether `retry` is offered.

- [ ] **Step 2: Write the failing retry tests**

Against the local stand-in, which can be told to fail a given number of times:

- `429` twice then `200` → one successful round, three attempts;
- `429` four times → failure after exactly three attempts;
- `401` → failure after **one** attempt, asserted by the stand-in's request
  count. Three attempts turn one clear failure into a three-times-slower
  identical failure;
- a read timeout produces `external_effect_unknown=True`, because the endpoint
  may have billed for a completion the platform never saw.

Backoff uses an injected sleep so the test asserts the delays without waiting
them.

- [ ] **Step 3: Implement**

Request building: the safety preamble, then the personality as a `system`
message, then the canonical messages. `stream` is not sent — design §3, and the
docstring says why rather than leaving it to look like an omission.

- [ ] **Step 4: Verify and commit**

---

### Task 8: The model policy union

**Files:**
- Modify: `agents/domain/models.py`, `agents/application/catalog.py`, `api/app.py`, `api/cli.py`, `shared/config.py`
- Modify: `packages/backend/tests/unit/agents/test_agent_spec.py`
- Create: `packages/backend/tests/integration/agents/test_endpoint_policy.py`

- [ ] **Step 1: Write the failing spec tests**

- an `openai_compatible` policy validates and normalizes;
- **every existing deterministic spec still validates, and its content hash is
  byte-for-byte what it was.** Pin an existing hash literal in the test: this is
  what lets `schema_version` stay `1`, and it is the claim that lets published
  versions stay untouched;
- `temperature` outside `[0, 2]` and `max_output_tokens` below 1 are refused;
- an unknown `provider` is refused by the discriminator rather than silently
  falling back to deterministic.

- [ ] **Step 2: Write the failing publish tests**

Against PostgreSQL:

- publishing against an endpoint that does not exist → `422`;
- against a `disabled` endpoint → `422`;
- with a Token limit against an endpoint whose `usage_quality` is `unavailable`
  → `422`, per technical design §9.4;
- with `max_output_tokens` above the endpoint's own → `422`, **not** silently
  clamped: a limit that quietly becomes a different limit is worse than an
  error.

- [ ] **Step 3: Implement**

The union, the publish-time validation in the Agent Catalog rather than in the
route, and a provider registry keyed on the discriminator, resolved once when
the Worker builds its dependencies.

- [ ] **Step 4: Verify and commit**

---

### Task 9: The console, at the minimum

**Files:**
- Modify: `apps/web/src/pages/AgentDetailPage.tsx`, `apps/web/src/api/types.ts`, both i18n files
- Modify: `apps/web/src/pages/AgentDetailPage.test.tsx`

The draft editor currently offers `模型场景` as though a deterministic scenario
were the only thing a model policy can be. Leaving it would make the console
misrepresent the platform, which is the one thing phase 2C committed it would
not do.

- [ ] **Step 1: Write the failing tests**

- a provider selector renders, and choosing 模型端点 swaps the scenario dropdown
  for an endpoint dropdown fed by `GET /model-endpoints`;
- a disabled endpoint is not offered;
- saving sends the union member the selection implies, asserted on the request
  body;
- when the endpoint list is empty, the option explains that no endpoint is
  registered rather than rendering an empty dropdown.

- [ ] **Step 2: Implement**

Nothing else. No endpoint-management UI: endpoints are registered over the API
by a platform administrator, and `docs/development.md` says so.

- [ ] **Step 3: Verify and commit**

---

### Task 10: Compose, CI, docs, and the exit record

**Files:**
- Modify: `deploy/compose/compose.yaml`, `.env.example`, `.github/workflows/ci.yml`, `docs/development.md`
- Create: `docs/superpowers/verification/2026-08-11-m1-real-model.md`

- [ ] **Step 1: Wire the settings**

The outbound and retry settings into `&app-env` with their defaults, and a
commented `TINY_HERMES_MODEL_KEY_EXAMPLE` in `.env.example` showing the
`credential_ref` convention. No real value anywhere.

- [ ] **Step 2: CI**

The new unit and integration directories are picked up by the existing paths, so
the only addition is the ban test's dependency on `ruff` being installed in the
integration job. Confirm the degraded-Redis run and the restart drill still pass
with the transcript change, because Task 6 touched the slice transaction that
both depend on.

- [ ] **Step 3: Documentation**

A model endpoint section in `docs/development.md`: registering one, the
`credential_ref` convention and why credentials are deployment configuration in
this slice, running the connectivity check, and publishing an Agent against it.
State plainly that Secret storage, streaming, token estimation, and tools do not
exist yet.

- [ ] **Step 4: The manual real-endpoint check**

Once, by hand, against a real OpenAI-compatible endpoint, with the result
recorded and the endpoint's identity and credential redacted: register, check,
publish, run, and read the Run's Token accounting. This is the only evidence
that the adapter works against something this repository did not write, and no
automated test may depend on it.

- [ ] **Step 5: Full fresh verification**

```bash
uv run --no-sync ruff check packages/backend migrations scripts
uv run --no-sync pyright
uv run --no-sync pytest packages/backend/tests -q
corepack pnpm --filter @tiny-hermes/web lint
corepack pnpm --filter @tiny-hermes/web test
corepack pnpm --filter @tiny-hermes/web build
docker compose -f deploy/compose/compose.yaml up -d --build --wait
corepack pnpm exec playwright test --config tests/e2e/playwright.config.ts
DETERMINISTIC_MODEL_DELAY_MS=3000 uv run --no-sync python scripts/restart_drill.py
```

- [ ] **Step 6: Write the verification record and commit**

Follow the 2C record's structure. State plainly, as that record did, that CI has
still never run; and state which outbound refusals are proven by test and which
by the single manual check.

---

## 3. Phase 3A completion checklist

- [ ] An Agent published against a registered endpoint completes a Run using
      text the endpoint produced, through the Worker.
- [ ] Two Runs in one persistent Session: the second sees the first's
      transcript. An ephemeral Session's does not.
- [ ] A failed round leaves no assistant message.
- [ ] Loopback, link-local, metadata, carrier-grade NAT, and unapproved private
      addresses are refused, in IPv4 and IPv6, at registration and at call time.
- [ ] A hostname resolving to one permitted and one forbidden address is
      refused.
- [ ] A redirect to a forbidden address is refused at that hop.
- [ ] A cross-origin redirect carries no `Authorization`.
- [ ] A resolver that changes its answer mid-request cannot move the connection.
- [ ] `ruff check` fails on a raw HTTP client outside the outbound module,
      proven by a test, and the exemption list is asserted to be exactly three
      paths.
- [ ] `429` is retried at most three times; `401` is not retried at all.
- [ ] `usage_quality=unavailable` fabricates no Token count and still enforces
      time and call-count limits.
- [ ] An AgentVersion with a Token limit cannot publish against an endpoint
      whose usage is unavailable.
- [ ] `estimated` is rejected by the endpoint schema.
- [ ] Every existing deterministic AgentSpec still validates with an unchanged
      content hash.
- [ ] No credential appears in any log, event payload, audit context, or API
      response.
- [ ] No `workspace_id` column on `model_endpoints`; no `secrets` table; no
      streaming; no tool binding.
- [ ] All phase 1, 2A, 2B, and 2C checks pass unchanged, including the restart
      drill.
