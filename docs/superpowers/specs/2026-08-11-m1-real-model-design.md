# M1 Real Model and Outbound Safety Design

> Date: 2026-08-11
>
> Status: written design for user review
>
> Delivery slice: M1 phase 3A of three

## 1. Purpose and authority

Phase two ended with a platform that executes Runs correctly and shows them in a
browser, and with exactly one model provider: a deterministic stand-in that
answers from a `match` statement and performs no network call. Every safety
property proven so far was proven against a provider that cannot fail in the ways
a real one does.

This slice makes an Agent talk to a real OpenAI-compatible endpoint, and it does
so through a client that treats every outbound address as hostile until proven
otherwise. It is the first time this platform sends a packet to somewhere it did
not start.

The following documents remain authoritative:

- `docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` v2.4;
- `docs/superpowers/specs/2026-08-09-tiny-hermes-m1-technical-design.md` v1.1,
  especially §9 (Agent kernel), §10.2 (two-step authorization), §12.3 (Secret),
  and §14 (SafeOutboundClient);
- `docs/superpowers/plans/2026-08-10-tiny-hermes-m1-roadmap.md` phase three;
- the phase 2A, 2B, and 2C designs for the seams this slice consumes.

Phase three in the roadmap is one block covering both the real model and the
Docker sandbox. This document proposes splitting it, and takes the first part.
The reason is not size alone: the model path and the sandbox path share no code
and no failure mode, and the outbound-safety work is the highest-risk piece in
all of M1 — it is the only place where a mistake reaches outside the machine.
Verifying it on its own, before Docker-in-Docker enters the test matrix, is worth
one extra slice boundary.

Two rules from the technical design govern this slice above the rest:

- **§14: 通过架构测试拒绝在规定模块外新建原始动态 HTTP 客户端或 socket.** The
  ban is machine-checked, not a review convention.
- **§9.4: `unavailable` 不伪造 Token 数.** When an endpoint does not report
  usage, the platform records that it does not know, and still enforces every
  limit that does not depend on knowing.

## 2. Observable outcome

After phase 3A, a platform administrator can register an OpenAI-compatible model
endpoint, and a workspace developer can publish an Agent that uses it. Concretely:

1. A platform administrator registers a model endpoint — base URL, model name,
   context window, maximum output, usage quality — and the platform refuses one
   whose address resolves anywhere it must not reach.
2. The administrator runs an explicit connectivity check against a registered
   endpoint and gets back a verdict, not a stack trace.
3. A workspace developer publishes an Agent whose `model_policy` selects that
   endpoint instead of a deterministic scenario, and the Agent Version schema
   refuses an endpoint that does not exist or is not approved.
4. A Run against that Agent reaches `completed` with text the model actually
   produced, and the Run's rounds accumulate into the Session transcript, so a
   second Run in the same Session sees what the first one said.
5. The Run's budget records real Token counts when the endpoint reports usage,
   and records `usage_quality=unavailable` — with Token limits inapplicable and
   every other limit still enforced — when it does not.
6. A model endpoint that rate-limits or drops the connection is retried at most
   three times with jittered backoff, within the same round, and a Run whose
   endpoint stays unreachable fails with a readable reason rather than hanging
   until the lease expires.
7. An endpoint whose host resolves to loopback, link-local, the cloud metadata
   address, carrier-grade NAT space, or an unapproved private network is refused
   — at registration, at every connection, and at every redirect hop.
8. A redirect that crosses origin does not carry the endpoint's credential.
9. A DNS record that changes between validation and connection cannot be used:
   the connection goes to the address that was vetted.
10. Adding `httpx.AsyncClient(...)` anywhere outside the outbound module fails
    the build.

The deterministic provider does not go away. It stays as a published, selectable
policy, because every test above phase 2B depends on a Run whose outcome is
known, and because an air-gapped installation must still be able to prove the
platform works.

## 3. Explicit non-goals

Not in this slice, and not to be implied by anything it builds:

- **No sandbox and no tools.** `file.*` and `shell.exec`, the Sandbox
  Controller, `/workspace/data` revisions, and Artifacts are phases 3B and 3C.
  `AgentSpec.tools` stays `tuple[()]` and the model is told about no tools, so a
  model that asks for one gets a normalized refusal, not a half-wired execution
  path.
- **No streaming to the caller.** The provider is called without `stream`, and
  the round's text lands as one block. Nothing in the platform consumes partial
  text yet: the console renders Run events, not tokens. Streaming arrives with
  Chat Completions in phase four, where it has a consumer. See §14.2.
- **No Secret storage.** Envelope encryption and KEK rewrapping are roadmap phase
  four. §6.3 explains what 3A does instead and why it is not a smaller version of
  the same thing.
- **No image or attachment input.** The CanonicalMessage block union has one
  member in this slice. §5.1 explains why the unused members are not declared
  ahead of a producer.
- **No provider failover.** Technical design §9.3: `M1 不做跨 Provider 自动切换`.
  Retries stay on the same endpoint.
- **No token estimation.** `usage_quality=estimated` requires a tokenizer
  verified to match the model. The platform has none, so it does not offer the
  value. See §8.2.
- **No console work beyond the minimum** needed so an endpoint-backed Agent can
  be published from the browser at all (§10).

## 4. What phase 2B left unfinished

`session_messages` exists, with a sequence, a role, a JSON content column, a
source Run, and a redaction flag. Run creation writes the user's message into it.
**Nothing has ever written an assistant message.** No code path in
`packages/backend/src` produces a row with `role = "assistant"`.

That went unnoticed because the deterministic provider does not need a
transcript. `_request` in the Worker passes `round_index` — the count of model
calls the Run has made — and the scenario branches on it. Round two of
`continue_once` does not need to know what round one said, because it was decided
in advance.

A real model needs the transcript, and the platform cannot produce one. This is
the largest change in the slice and the least visible from the roadmap, so it is
stated first:

- A completed model round appends an assistant message to the Session, in the
  same transaction that records the slice.
- The next round builds its request from the Session's messages, not from
  `input_text` and a counter.
- A Session's second Run therefore begins with the first Run's conversation
  behind it, which is what `session_mode=persistent` has been promising since
  phase 2A without anything standing behind it.

Ordering follows the existing `sessions.next_message_sequence` allocator, which
already exists and is already exercised — the same mechanism as
`runs.next_event_sequence`, and it needs no new concurrency reasoning.

## 5. The provider seam

### 5.1 CanonicalMessage

Phase 2B's `ModelRequest` carries `policy`, `personality`, `input_text`, and
`round_index`. That shape cannot express a conversation, so it is replaced:

```python
@dataclass(frozen=True)
class TextBlock:
    text: str

Block = TextBlock  # widened to a tagged union in phase 3C

@dataclass(frozen=True)
class CanonicalMessage:
    role: Literal["user", "assistant"]
    blocks: tuple[Block, ...]

@dataclass(frozen=True)
class ModelRequest:
    policy: ModelPolicy
    personality: str
    messages: tuple[CanonicalMessage, ...]
    round_index: int
```

`Block` is an alias for one type rather than a union with one member, and the
tool-call and tool-result blocks the technical design names are **not declared
here**. A union with unreachable members is a promise the platform cannot keep,
and the roadmap rule is explicit: `不能把未实现行为伪装为可用`. Phase 3C widens
the alias into a tagged union when it has a producer; every `match` on it will
fail typechecking at that point, which is exactly the review the widening
deserves.

`round_index` survives the rewrite because the deterministic provider branches on
it and because the budget already counts model calls. It is derived, not stored.

### 5.2 The port

```python
class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
```

is unchanged in shape. `ModelResponse` gains what a real provider knows and the
stand-in does not:

| Field | Meaning |
|---|---|
| `stop_reason` | as today: `completed`, `continue`, `failed` |
| `text` | the round's assistant text |
| `model_calls` | as today |
| `input_tokens`, `output_tokens` | `None` when the endpoint reports nothing |
| `usage_quality` | `provider` or `unavailable` |
| `replay_safe`, `external_effect_unknown` | as today |
| `failure` | a normalized reason when `stop_reason` is `failed` |

`tokens: int = 0` is replaced by the nullable pair. Zero is a number the platform
would then have to distinguish from "not reported", and it has been getting away
with conflating them only because the stand-in always reports.

Two providers implement the port: `DeterministicModelProvider`, unchanged in
behavior, and `OpenAICompatibleProvider`. Selection is by the published Agent
Version's `model_policy.provider`, exactly as today — a registry keyed on the
discriminator, resolved once when the Worker builds its dependencies.

### 5.3 Normalization the adapter owns

Per technical design §9.2, and no more than this slice can honour:

- **System message placement.** The personality becomes a `system` message; the
  server-side safety preamble precedes it. The preamble is fixed text in this
  slice, not configurable.
- **Stop reason.** `stop` → `completed`; `length` → `failed` with reason
  `max_output_reached`; `tool_calls` → `failed` with reason
  `tool_use_not_supported`, because no tool is bound and a model that asks for
  one has left the contract; anything unrecognized → `failed` with reason
  `unsupported_stop_reason`, never silently treated as success.
- **Usage.** `usage.prompt_tokens` / `completion_tokens` when present, otherwise
  `usage_quality=unavailable`.
- **Content.** A response with no text content is `failed` with reason
  `empty_response`, not a completed round with an empty answer.

Every one of those failures is a `stop_reason=failed` with `replay_safe=True`:
nothing was written anywhere, so the Run is safely retriable. `replay_safe` is
`False` only for a request whose response never arrived (§9.3).

## 6. Model endpoints

### 6.1 The table

`model_endpoints`, platform-scoped, with no `workspace_id`:

| Column | Notes |
|---|---|
| `id` | |
| `name` | unique, human-chosen |
| `kind` | `openai_compatible`, the only value in M1 |
| `base_url` | scheme + host + optional port + optional path prefix |
| `model` | the provider's model identifier |
| `context_window`, `max_output_tokens` | hard ceilings, enforced above the AgentVersion's own |
| `usage_quality` | `provider` or `unavailable`, declared by the administrator |
| `credential_ref` | see §6.3 |
| `status` | `active`, `disabled` |
| `created_by`, timestamps | |

Platform-scoped because the technical design assigns approval to the platform
administrator: `平台管理员可批准企业私有模型端点；Workspace 管理员只能在已批准
范围内选择`. A workspace does not register endpoints; it selects from those that
exist. Per-workspace restriction of that list is a phase-four concern and no
column is reserved for it here.

### 6.2 Routes

```text
GET    /api/v1/model-endpoints          any workspace member; returns no credential
POST   /api/v1/model-endpoints          platform administrator
PATCH  /api/v1/model-endpoints/{id}     platform administrator
POST   /api/v1/model-endpoints/{id}/check   platform administrator
```

The list is readable by any workspace member because the Draft editor has to
offer it. It returns `id`, `name`, `model`, `context_window`,
`max_output_tokens`, `usage_quality`, and `status` — never the credential and
never its reference.

`POST .../check` is the connectivity test technical design §14 requires to go
through SafeOutboundClient. It sends one minimal completion request and answers
with a verdict: reachable, refused by outbound policy, unauthorized, or
unreachable, plus the round-trip milliseconds. It never echoes the response body,
because a misconfigured `base_url` pointing at some internal service would
otherwise turn the check into a read primitive.

Registration validates the address before the row is written. A `base_url` that
resolves somewhere §7 forbids is a `422` with the reason, not a row that fails
later inside a Run.

### 6.3 Credentials, and what this slice deliberately does not build

An OpenAI-compatible endpoint needs a bearer token. The technical design's answer
is the `secrets` table with a per-Secret DEK wrapped by a deployment KEK (§12.3),
and the roadmap puts that work — with its rewrap drill — in phase four.

Building half of it now would be worse than not building it. A `secrets` table
holding plaintext, or holding ciphertext under a key with no rotation path, is a
security control that reads as present and is not.

So 3A stores no credential at all. `credential_ref` names an environment
variable that the deployment provides to the Worker and the API:

```text
credential_ref = "TINY_HERMES_MODEL_KEY_ACME"
```

The platform reads it at call time, never writes it anywhere, never returns it
over any route, and never logs it. Registration fails if the named variable is
absent from the API process, so a broken configuration is found at registration
rather than inside someone's Run.

This is a real limitation and it is recorded as one: it means model credentials
are deployment configuration, not workspace data, and rotating one is a restart.
Phase four replaces `credential_ref` with a Secret reference; the column is named
`credential_ref` rather than `api_key_env` so that replacement is a change of
interpretation, not a migration of every row.

## 7. SafeOutboundClient

One module, `packages/backend/src/tiny_hermes/outbound/`. Everything that leaves
the machine goes through it.

### 7.1 What it refuses

Resolution happens first, and **every** address a hostname resolves to is
checked, not the first one. Refused unless explicitly approved:

| Class | Examples |
|---|---|
| Loopback | `127.0.0.0/8`, `::1` |
| Link-local | `169.254.0.0/16`, `fe80::/10` — this covers the EC2/GCP metadata address |
| Carrier-grade NAT | `100.64.0.0/10` — covers the Alibaba Cloud metadata address |
| Unique-local and private | `10/8`, `172.16/12`, `192.168/16`, `fc00::/7` |
| Unspecified, multicast, reserved | `0.0.0.0`, `224/4`, everything `ipaddress` calls reserved |

A platform administrator may approve specific private CIDRs through
configuration, which is how an enterprise private endpoint becomes reachable. The
allowlist is CIDRs, never hostnames: a hostname allowlist is checked before
resolution and is therefore not a control.

Also refused: any scheme other than `https`, except `http` to an explicitly
approved CIDR — because an on-premises endpoint on a private network is the one
place plaintext is a deliberate operator choice rather than an accident.

### 7.2 Pinning

The address that was vetted is the address that is connected to. The client
resolves, vets, picks one address, and issues the request against that literal
IP with the `Host` header and TLS SNI set to the original hostname. A record that
changes between the check and the connect cannot be used, which is the whole of
the DNS-rebinding defence.

In `httpx` terms this is a custom `AsyncHTTPTransport` subclass that rewrites
`request.url.host` to the pinned address and sets `extensions["sni_hostname"]`.
Certificate verification still validates against the hostname, so pinning does
not weaken TLS.

### 7.3 Redirects

`follow_redirects=False`, and the client drives the hops itself — at most five.
Each hop re-resolves, re-vets, and re-pins. A hop that changes scheme, host, or
port drops the `Authorization` header before sending.

This is the case that a library's redirect follower gets wrong by default, and it
is the reason the client cannot simply be `httpx` with careful arguments.

### 7.4 The architecture ban

Ruff's `flake8-tidy-imports` banned-API rules, with a per-file exemption for the
outbound module only:

```toml
[tool.ruff.lint.flake8-tidy-imports.banned-api]
"httpx.AsyncClient".msg = "Outbound requests go through tiny_hermes.outbound."
"httpx.Client".msg = "..."
"socket.socket".msg = "..."
"urllib.request.urlopen".msg = "..."
```

Lint, not a test, because it runs on every file on every commit and names the
offending line. A test additionally asserts that the exemption list contains
exactly the outbound module, so widening the exemption is a visible diff rather
than a quiet one.

`scripts/` and the test tree are exempt: the restart drill and the integration
suite are clients of the platform, not the platform.

### 7.5 Budgets

Per request: 5s connect, 60s read, 5 redirects, 10 MiB response cap. The response
cap matters — an endpoint that answers with a gigabyte should fail the round, not
the Worker.

## 8. Usage, limits, and retry

### 8.1 Where a model call sits in the slice

The slice budget (`worker_max_slice_seconds`, default 30) is checked **between**
rounds and never inside one, so a single model call is bounded only by the
outbound read timeout. That is correct and worth stating: a round is atomic with
respect to the slice, and a 45-second model call inside a 30-second slice budget
simply means the slice ends after it.

The lease is not at risk, because the Worker's renewal loop runs concurrently
with the round and has since phase 2B. The restart drill's third scenario is the
proof that lease expiry is what recovers a Worker that stops renewing.

### 8.2 Usage quality

- `provider`: the endpoint reported `usage`. Token counts accumulate into the
  shared budget and Token limits are enforced.
- `unavailable`: no counts. `consumed_tokens` is not incremented and the
  Run's budget records that its Token accounting is incomplete. Time limits,
  model-call counts, and per-response size limits are enforced unchanged.

`estimated` is not implemented. §9.4 permits it only with `经验证匹配该模型的
tokenizer`, and the platform has no verified tokenizer for any model, so offering
the value would be offering a number the platform cannot stand behind.

An AgentVersion that declares a Token limit cannot select an endpoint whose
`usage_quality` is `unavailable`. The Agent Version schema enforces this at
publish, which is where the technical design puts it: `要求严格 Token 上限的
AgentVersion 不能选择 usage 不可用的端点`.

### 8.3 Retry within a round

Technical design §9.3: at most three attempts, exponential backoff with jitter,
same endpoint, and **only while the round has produced no visible output and no
side effect**. Because 3A does not stream and binds no tools, that condition is
always satisfiable and the reasoning is simple: an attempt either produced a
complete response or produced nothing.

Retried: HTTP 429, 500, 502, 503, 504, connect failures, read timeouts.
Not retried: 400, 401, 403, 404, 422 — a request the endpoint rejected on its
merits will be rejected again, and three attempts turn one clear failure into a
three-times-slower identical failure.

A request whose response never arrived — a read timeout after the request was
sent — sets `external_effect_unknown=True` on the eventual failure, because the
endpoint may have billed for a completion the platform never saw. That flows into
the existing `checkpoint_replay_safe` machinery from phase 2A, which already
decides whether `retry` is offered.

Backoff is bounded so that three attempts cannot outlast the outbound read
timeout budget by more than a small constant; a Run that is going to fail should
fail while someone is still watching.

## 9. Agent spec

`model_policy` becomes a union discriminated on `provider`:

```python
class DeterministicModelPolicy(BaseModel):   # unchanged
    provider: Literal["deterministic"] = "deterministic"
    scenario: Literal["complete", "fail_replay_safe", "continue_once"] = "complete"

class EndpointModelPolicy(BaseModel):
    provider: Literal["openai_compatible"]
    endpoint_id: UUID
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1)

ModelPolicy = Annotated[
    DeterministicModelPolicy | EndpointModelPolicy, Field(discriminator="provider")
]
```

`schema_version` stays `1`. The change is a widening: every spec that validated
before still validates, and its normalized JSON and content hash are byte-for-byte
what they were, so no published version is disturbed and no migration of
`agent_versions` is needed. A version bump would be right for a narrowing or a
rename; it is not right for accepting more.

Publish-time validation, in the Agent Catalog and not in the route: the endpoint
must exist, be `active`, and satisfy §8.2's Token-limit rule. `max_output_tokens`
above the endpoint's own is refused rather than silently clamped — a limit that
quietly becomes a different limit is worse than an error.

## 10. Console, at the minimum

The Draft editor currently offers `模型场景` as though a deterministic scenario
were the only thing a model policy can be. Leaving it that way would make the
console misrepresent the platform, which is the one thing phase 2C committed it
would not do.

The minimum change: a provider selector, and beneath it either the existing
scenario dropdown or an endpoint dropdown fed by `GET /model-endpoints`. Nothing
else. The full Agent Builder is phase four, and no endpoint-management UI is
built in this slice — endpoints are registered over the API by a platform
administrator, and `docs/development.md` documents that.

## 11. Configuration

| Setting | Default | Notes |
|---|---:|---|
| `outbound_connect_timeout_seconds` | 5 | |
| `outbound_read_timeout_seconds` | 60 | one model round's ceiling |
| `outbound_max_redirects` | 5 | |
| `outbound_max_response_bytes` | 10485760 | |
| `outbound_allowed_cidrs` | `""` | comma-separated; approves private ranges |
| `model_max_attempts` | 3 | workspace may lower, never raise |
| `model_retry_base_ms` | 250 | jittered exponential |

Every bound explicit, matching the phase-2B settings style, so an operator cannot
configure a read timeout longer than a lease can be renewed through.

## 12. Failure and transaction behavior

- The model call happens **outside** the database transaction that records the
  slice, as it does today. A network call inside a transaction holds a connection
  for the duration of somebody else's outage.
- An assistant message and the slice record commit together. A round whose text
  was produced but not recorded must not be able to leave the transcript short,
  because the next round would then build a different conversation.
- A failed round writes no assistant message. The transcript contains what the
  Agent said, never what it tried to say.
- Outbound refusals are `failed` with a `run_failed` event carrying the refusal
  code, and the refused address is recorded in the audit event but **not** in the
  run event payload, which a workspace member can read: a resolved internal IP is
  exactly the sort of thing an outbound-policy probe would be fishing for.
- The credential never enters a log line, an event payload, an audit context, or
  an exception message. The outbound module's request logging records method,
  scheme, host, port, status, and elapsed time.

## 13. Verification strategy

### 13.1 Fast domain tests, no network

The address policy is a pure function over `ipaddress` objects, so the refusal
table is a table-driven unit test: every class in §7.1, IPv4 and IPv6, plus the
approved-CIDR path. This is the security control, and it is testable without a
socket.

The adapter's normalization is likewise pure over recorded response bodies: stop
reasons, usage present and absent, empty content, unrecognized fields.

### 13.2 Against a local HTTP server, no external network

A real `uvicorn` server on `127.0.0.1` standing in for a model endpoint — the
same technique phase 2B used for SSE, for the same reason: a transport that
buffers or shortcuts proves nothing about a client.

Loopback is refused by policy, which is the point: these tests instantiate the
client with `127.0.0.0/8` in the approved CIDRs, which proves the allowlist works
*and* keeps the default-deny behavior under test elsewhere. The redirect,
rebinding, and header-stripping tests all run here:

- a redirect to a forbidden address is refused at the hop, not followed;
- a cross-origin redirect arrives at the second server with no `Authorization`;
- a resolver that answers with a permitted address and then a forbidden one
  still connects to the permitted address — the rebinding case, driven by a stub
  resolver, since it cannot be arranged with real DNS.

### 13.3 Integration, against PostgreSQL

Endpoint registration and its refusals; publish-time validation of
`EndpointModelPolicy`; the transcript accumulating across two Runs in one
persistent Session; a Run completing against the local stand-in endpoint with
`usage_quality=provider`, and another with `unavailable` and a budget that says
so.

### 13.4 The architecture ban

`ruff check` fails on a file that constructs `httpx.AsyncClient` outside the
outbound module — asserted by a test that runs ruff against a temporary file, so
the ban is proven to bite rather than merely configured.

### 13.5 What is not tested here

No test in this slice calls a real vendor endpoint. CI has no credential and must
not need one, and a suite that depends on someone else's uptime is a suite that
goes red for reasons unrelated to this repository. The real-endpoint check is a
documented manual step with a recorded result in the verification record, in the
same way the restart drill's timings are recorded.

## 14. Deliberate deferrals

1. **No Secret storage** (§6.3). Credentials are deployment environment
   variables. Phase four replaces the reference.
2. **No streaming** (§3). Nothing consumes partial text; phase four's Chat
   Completions does, and the merge path is built where it can be tested against a
   consumer.
3. **No `estimated` usage quality** (§8.2). No verified tokenizer exists.
4. **No per-workspace endpoint restriction** (§6.1). Endpoints are platform-wide
   and any workspace may select any active one.
5. **The block union has one member** (§5.1). It widens in 3C, where the compiler
   will demand every `match` be revisited.

## 15. Exit criteria

- [ ] An Agent published against a registered endpoint completes a Run using text
      the endpoint produced, end to end through the Worker.
- [ ] Two Runs in one persistent Session: the second sees the first's transcript.
- [ ] Loopback, link-local, metadata, CGNAT, and unapproved private addresses are
      refused, in IPv4 and IPv6, at registration and at call time.
- [ ] A redirect to a forbidden address is refused at that hop.
- [ ] A cross-origin redirect carries no `Authorization`.
- [ ] A resolver that changes its answer mid-request cannot move the connection.
- [ ] `ruff check` fails on a raw HTTP client constructed outside the outbound
      module, proven by a test.
- [ ] An endpoint returning 429 is retried at most three times and then fails
      readably; a 401 is not retried at all.
- [ ] `usage_quality=unavailable` fabricates no Token count and still enforces
      time and call-count limits.
- [ ] An AgentVersion with a Token limit cannot publish against an endpoint whose
      usage is unavailable.
- [ ] No credential appears in any log, event payload, audit context, or API
      response.
- [ ] Every phase 1, 2A, 2B, and 2C check passes unchanged, including the
      restart drill.

### Next seams

Phase 3B consumes: the provider registry, so a sandboxed tool call can be
selected the same way a provider is; the assistant-message transcript, which tool
calls and results extend; and the outbound module, which the Sandbox Controller
must **not** use — its Docker socket is a local seam with its own rules, and
reusing an outbound HTTP client for it would put a control designed for hostile
addresses in charge of a trusted one.

### 15.1 `/v1/responses`: why not in 3A, and what would change it

3A speaks `/v1/chat/completions`, and 3C should too. This section exists so the
question is not re-argued from scratch, and because part of the reasoning was
settled by measurement rather than by opinion.

**What was measured.** The endpoint used in the phase-3A manual check (§6 of the
verification record) was probed on both paths. Both answered `200` — and the
`chat.completions` response carried an `id` beginning `resp_`, which says the
gateway is Responses-native and emulates Chat Completions on top of it. So for
that endpoint, the path this platform uses is the *translated* one. The
assumption that Responses is not yet widely available did not survive contact:
vLLM and Ollama serve it too.

**Why 3A still does not use it.** Not a technical judgement — a process one. That
slice is committed, verified, and proven against a real endpoint. Changing it
means repeating the whole verification for zero present capability, which is a
certain cost against an uncertain gain.

**The argument that does survive.** Responses' central offering is server-side
state: `store` with `previous_response_id`. This platform took ownership of the
conversation in phase 3A, in the same transaction that records a slice, and its
entire recovery model rests on that ownership. Consider a Worker killed
mid-round: the lease expires, the Scheduler requeues the Run, and a new Worker
picks it up — which `previous_response_id` does it continue from, and did that
round complete at the provider or not? The platform cannot know, and the failure
lands exactly where recovery is supposed to work.

**So any adoption is stateless.** `store=false`, with the full transcript sent
from `session_messages` every round, as today. That constraint is not negotiable
without redesigning recovery.

**Which reduces the real question to one thing.** Stateless Responses is Chat
Completions plus reasoning-item passing plus a different serialization. The net
gain is reasoning items — which matter, because dropping them between rounds
costs quality and prompt-cache hits on reasoning models, and matters most in the
multi-round tool loops phase 3C introduces. That is a measurable claim, not a
preference.

**When to do it.** Phase 3B or 3C, as a second `kind` alongside
`openai_compatible` — a schema widening, a second `normalize`, and a router
branch. `ModelProvider` does not move; this is the same shape of change as
adding `EndpointModelPolicy` was. Chat Completions stays, because it remains the
protocol every provider implements and this platform registers other people's
endpoints rather than one vendor's.

**How to decide it.** Not by reading a changelog. Run one Agent and one
conversation through both `kind`s against a reasoning model and compare answer
quality and Token consumption. If the numbers do not move, the older path is the
one with wider coverage and it stays.
