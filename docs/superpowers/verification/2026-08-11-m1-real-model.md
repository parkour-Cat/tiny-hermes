# M1 Real Model Verification Record

> Date: 2026-08-11
>
> Slice: M1 phase 3A of three (phase three, split — see §5.1)
>
> Branch: `m1-real-model` in `.worktrees/m1-run-execution`
>
> Verified at commit: `056bcc7` (this record and the Compose, CI, and
> documentation changes commit on top of it)

## 1. Commits in the slice

| Commit | Subject |
|---|---|
| `55d6cc0` | docs: design the phase-3A real model slice |
| `db42fa0` | docs: plan the phase-3A real model slice file by file |
| `902de0e` | feat: decide which addresses may be connected to |
| `7e1a2af` | feat: make outbound calls through one guarded client |
| `63c571c` | ci: ban raw HTTP clients outside the outbound module |
| `0f3d700` | feat: register the model endpoints a platform may reach |
| `99d1fb0` | feat: manage model endpoints over the API |
| `870b5c7` | feat: keep what the agent said where the next round can read it |
| `a9041af` | feat: call an OpenAI-compatible endpoint as a model provider |
| `15c1836` | feat: let an agent select a real model endpoint |
| `056bcc7` | feat: choose a model provider in the draft editor |

## 2. Environment

| Component | Version |
|---|---|
| Python | 3.12.6 |
| uv | 0.11.26 |
| httpx | 0.28.1 (a **runtime** dependency as of this slice — see §7.1) |
| pytest | 9.1.1 |
| PostgreSQL | 17.6 (Compose `postgres` service) |
| Redis | 8.2.1 (Compose `redis` service) |
| Docker Engine | 29.5.3 |
| Node | 24.6.0 |
| pnpm | 10.15.0 |
| Vitest | 4.1.10 |
| Playwright | 1.62 |

Every database command targeted `tiny_hermes_test` only. Database URLs, cookies,
CSRF tokens, session tokens, bootstrap tokens, passwords, model endpoint
credentials, Run input, and Agent personality text are absent from this record.

The browser and drill runs used the isolated `-p tiny-hermes-e2e` Compose
project, for the reason recorded in the phase-2C record. No volume was deleted.

## 3. Commands and results

### 3.1 Static checks and the whole backend suite

```text
uv run --no-sync ruff check packages/backend migrations scripts  All checks passed!
uv run --no-sync pyright                                         0 errors, 0 warnings
uv run --no-sync pytest packages/backend/tests -q                432 passed in 153.49s
```

432 = 250 unit + 182 integration. Phase 2C ended at 271; this slice adds 161.

### 3.2 Migration round trip

```text
uv run --no-sync alembic downgrade 20260810_0003 / upgrade head   ok
uv run --no-sync alembic downgrade 20260810_0002 / upgrade head   ok
uv run --no-sync alembic downgrade 20260810_0001 / upgrade head   ok
uv run --no-sync alembic downgrade base          / upgrade head   ok
uv run --no-sync alembic check                   No new upgrade operations detected
```

### 3.3 The suite with the wake-up channel unreachable

```text
REDIS_URL=redis://127.0.0.1:6399/0 pytest packages/backend/tests/integration -q -rs
  177 passed, 5 skipped in 277.79s
```

The five skips are the wake-up optimization's own tests, unchanged from phase 2B.

### 3.4 Web checks

```text
corepack pnpm --filter @tiny-hermes/web lint    eslint . --max-warnings 0, no output
corepack pnpm --filter @tiny-hermes/web test    10 files, 69 tests passed
corepack pnpm --filter @tiny-hermes/web build   built in 620ms
```

### 3.5 Browser acceptance against the Compose stack

```text
docker compose -f deploy/compose/compose.yaml up -d --build --wait   all services healthy
corepack pnpm exec playwright test --config tests/e2e/playwright.config.ts

  ok 1 [setup]      bootstrap the platform and sign in                     (3.8s)
  ok 2 [foundation] login, create two workspaces and logout                (7.2s)
  ok 3 [console]    draft, publish, submit, watch, retry, and be refused
                    a foreign workspace                                   (20.3s)
  ok 4 [console]    the panes phase two cannot fill are absent, not empty (10.5s)
  ok 5 [console]    an EventSource-shaped subscription is served, and an
                    unscoped one is refused                                (1.2s)

  5 passed (52.3s)
```

### 3.6 Restart drill, after the transcript change

Task 6 rewrote the slice transaction that all three drill scenarios depend on, so
the drill is evidence about this slice and not only about the last one.

```text
COMPOSE_PROJECT_NAME=tiny-hermes-e2e DETERMINISTIC_MODEL_DELAY_MS=3000
uv run --no-sync python scripts/restart_drill.py

1. Worker restarted while executing
  after restart     status=completed  seconds=28.25
  events            count=6  contiguous=True  leases=2  last=run_completed
2. Redis stopped, then restarted
  without redis     status=completed  pickup=0.47  seconds=6.11
  events            count=3  contiguous=True  leases=1  last=run_completed
  with redis #1     pickup=0.12    #2  pickup=2.08    #3  pickup=2.03
3. Worker killed holding a lease, Scheduler restarted
  lease expired     status=queued     seconds=26.38
  worker returned   status=completed  seconds=2.44
  events            count=6  contiguous=True  leases=2  last=run_completed

All three scenarios held. 125.0s
```

The Redis figures are worth reading: `0.12, 2.08, 2.03`. Two of the three Runs
missed their wake-up and waited a poll. The phase-2C decision to assert the
*minimum* of three rather than a single sample is what kept this green, and a
single-sample assertion would have failed here on a healthy platform. Recorded
because it is the second time that decision has paid.

### 3.7 Secret scanning

`git grep` for secret-shaped values (AWS keys, GitHub tokens, `sk-` keys, PEM
private keys) returned no matches. The only tracked environment file is
`.env.example`, whose new entries are commented-out variable *names*.

## 4. What the slice proves

- **An Agent can talk to a real model.** A registered endpoint, a published
  Agent that names it, a submitted Run, and a Worker holding nothing but a
  `ModelRouter` — the Run reaches `completed` with text the endpoint produced,
  and that text lands in the Session transcript. One integration test walks the
  whole path.
- **The outbound path refuses everything it must, and proves it without a
  network.** Loopback, link-local (AWS and GCP metadata), carrier-grade NAT
  (Alibaba Cloud metadata), unique-local, private and reserved, in IPv4 and
  IPv6, as a table-driven test over `ipaddress` objects. A hostname resolving to
  one permitted and one forbidden address is refused, which is the bug the
  function exists to prevent.
- **Vetting and connecting cannot drift apart.** A stub resolver that answers
  with a permitted address and then a forbidden one still produces a connection
  to the permitted one, because the client dials the literal it checked. This is
  invisible in every other test and is the whole of the rebinding defence.
- **A redirect is a new request, not a continuation.** A hop into the metadata
  address is refused *at that hop* and the stand-in never sees the second
  request; a cross-origin hop arrives with no `Authorization`, asserted on what
  the second server received.
- **The ban bites.** `ruff` fails on `httpx.AsyncClient` written under the source
  tree, proven by a test that runs the linter, and a second test pins the
  exemption list at exactly three paths.
- **A Session became a conversation.** A completed round appends what the Agent
  said, in the slice transaction; a failed round appends nothing; a persistent
  Session's second Run is handed the first Run's transcript and an ephemeral
  one's is not — the first behaviour `session_mode` has ever had.
- **Usage is reported, never invented.** Both Token counts or neither. An
  endpoint that reports nothing yields `usage_quality=unavailable`, adds nothing
  to `consumed_tokens`, and says so on the Run snapshot, while time and
  model-call limits are enforced unchanged.
- **Retries cost what they should.** `429` is retried three times with jittered
  backoff; `401` is not retried at all, asserted by the stand-in's request count
  because that is where a wasted call lands.
- **The platform stores no model credential.** No column, no route, no log. The
  tests assert the absence of the keys rather than the absence of the values.
- **Publishing widened without disturbing anything.** The canonical deterministic
  spec still hashes to `4fcf412e…`, pinned as a literal, which is the whole
  argument for leaving `schema_version` at 1.

## 5. Deliberate deviations from the plan and design

1. **Phase three was split, and this is 3A.** The roadmap treats the real model
   and the Docker sandbox as one phase. They share no code and no failure mode,
   and outbound safety is the only place in M1 where a mistake reaches outside
   the machine — worth verifying before Docker-in-Docker enters the test matrix.
   3B is the sandbox; 3C is tools and files.

2. **Loopback cannot be approved, so the tests inject the policy.** An operator
   approving `0.0.0.0/0` to reach one private endpoint must not thereby open the
   machine to itself, so only private and carrier-grade NAT ranges are
   approvable. That closed the route a stand-in server needs, so the client takes
   the address policy as a collaborator and the tests relax it in one named way.
   `test_consults_the_real_policy` uses the real policy and asserts the stand-in
   recorded *no request at all*, so a relaxed test cannot conceal a client that
   connected first and asked afterwards.

3. **The address is not validated when an endpoint is registered.** The design
   document said it would be. Address safety is enforced at call time, on every
   request and every hop, by a check that cannot be bypassed; a second check at
   registration is a convenience that goes stale the moment DNS changes, and two
   rules for one question are two rules that drift. The `check` route gives the
   same early feedback down the code path the Worker will actually take — and as
   a side effect its tests need neither DNS nor a network, because a literal
   address is enough.

4. **§9.4's Token-limit rule is not implemented.** `AgentLimits` has no Token
   field and `run_budget_scopes.max_tokens` is NULL for every Run, so the rule
   would be a branch nothing can reach. Adding the field changes the normalized
   document and therefore the content hash, so the Token limit and the ability to
   read more than one `schema_version` have to arrive together. A test asserts
   the field does not exist and fails the moment somebody adds it, which is
   exactly when the rule has to be written.

5. **`CanonicalMessage` stays `role` + `text`, not a list of blocks.** The design
   document specified `blocks: tuple[Block, ...]`. Every message the platform can
   produce has exactly one text part, so a list would be structure with no
   producer — the same argument that keeps the tool-call and tool-result block
   types undeclared. The stored document already carries a `parts` list, so 3C
   widens this class without touching a row.

6. **No streaming, asserted rather than merely omitted.** A test checks that
   `stream` is absent from the request body, so the omission reads as a decision
   rather than a forgotten default.

7. **The endpoint list is readable by any signed-in user, and carries no
   `base_url`.** The draft editor has to offer it. It holds no tenant data — only
   which models the platform offers — but an internal model host is a piece of
   network map, so the address appears only in the administrator detail, and the
   `credential_ref` appears nowhere at all. `credential_available` is the fact an
   administrator actually needs.

8. **The safety preamble is fixed text.** A configurable one is a policy surface,
   and there is nowhere to administer it yet.

9. **`AgentCatalog` takes the endpoint store as an optional collaborator.** The
   in-memory adapter and the fast domain tests know nothing about endpoints, and
   a deterministic Agent never reaches the check. An endpoint-backed Agent cannot
   be published without a catalog to check against, which is the case that
   matters.

## 6. Known phase-3A limits

- **No Secret storage.** A model credential is a deployment environment variable
  named by `credential_ref`. Rotating one is a restart, and a workspace cannot
  hold its own key. Phase four.
- **No streaming.** Nothing consumes partial text; Chat Completions in phase four
  will.
- **No `estimated` usage quality**, and therefore no Token accounting at all for
  an endpoint that reports none.
- **No Token limit** — see §5.4.
- **No tools, no sandbox, no file handling.** `AgentSpec.tools` is still `()`.
- **No per-workspace endpoint restriction.** Endpoints are platform-wide and any
  workspace may select any active one.
- **No endpoint-management UI.** Registration is an API operation.
- **The platform still has exactly one user.** Bootstrap creates the first
  administrator and no route creates another; ServiceAccounts and API keys are
  phase four. The tests that need a non-administrator seed one directly into the
  database, reusing the pattern the phase-2A agent tests introduced.
- **CI has still never run.** `git remote -v` is empty. Every CI claim rests on
  the same steps having been reproduced locally, in the same order, with the
  results above. §7.1 is what that costs.

### The real-endpoint check has not been performed

The plan's Task 10 Step 4 is a single manual run against a real
OpenAI-compatible vendor endpoint — register, check, publish, run, read the
Token accounting — with the endpoint and credential redacted. **It has not been
done.** No credential is available in this environment, and obtaining one and
sending a request to a third-party service is the repository owner's call rather
than something to arrange unasked.

Everything the adapter does is proven against a local stand-in that speaks the
same protocol, and the outbound client is proven against a real socket. What
remains unproven is the one thing a stand-in cannot establish: that a real
vendor's responses fit the shapes `normalize` expects. Until that run happens,
treat the OpenAI-compatible adapter as verified against a specification and not
against a vendor.

## 7. Redacted failure evidence

1. **The API container would not start, and no test could have caught it.**
   `httpx` was a dev dependency; `tiny_hermes.outbound.client` imports it and is
   production code. Every local check passed — the dev environment has httpx
   installed — and the Compose image, which installs runtime dependencies only,
   died with `ModuleNotFoundError: No module named 'httpx'` on the first
   `up --build`. Fixed by moving it into `[project].dependencies` and relocking.
   Recorded prominently because it is the clearest argument this repository has
   for keeping a real container build in the verification list: a dependency
   group is a claim about what production needs, and only a production build
   checks it.

2. **The first version of the drill's Redis assertion would have failed here.**
   Not a defect in this slice, but §3.6's numbers are the second confirmation of
   a phase-2C decision, so they are recorded rather than glossed.

3. **The stand-in endpoint counted uvicorn's lifespan startup as a request.**
   Every "how many calls did the endpoint see" assertion was off by one, and the
   symptom was an empty `{}` in the recorded bodies. The ASGI app now
   acknowledges the lifespan scope instead of parsing it. Worth recording because
   the failure looked like a retry bug and was a test-harness bug.

4. **Ambiguous test queries, twice, for the same underlying reason.** Ant Design
   writes the selected option's text into a `title` attribute, and Testing
   Library's `findByLabelText` falls back to `title` — so a select *showing*
   "模型端点" answered to that name as well as the field actually called that.
   The same collision made `findByTitle("acme-gpt")` match both the closed select
   and its dropdown option. Fixed by querying selects by role and accessible
   name, and options inside `.ant-select-dropdown`. This is the same rc-select
   trap the phase-2C Playwright specs hit, in a second framework.

5. **Selecting `TID` enabled a rule this slice had no business opening.**
   `TID252` objects to relative imports from parent modules, which six existing
   test files use. Narrowed to `TID251`.

## 8. Phase-3A completion checklist

- [x] An Agent published against a registered endpoint completes a Run using
      text the endpoint produced, through the Worker.
- [x] Two Runs in one persistent Session: the second sees the first's
      transcript. An ephemeral Session's does not.
- [x] A failed round leaves no assistant message.
- [x] Loopback, link-local, metadata, carrier-grade NAT, and unapproved private
      addresses are refused, in IPv4 and IPv6.
- [x] A hostname resolving to one permitted and one forbidden address is
      refused.
- [x] A redirect to a forbidden address is refused at that hop.
- [x] A cross-origin redirect carries no `Authorization`.
- [x] A resolver that changes its answer mid-request cannot move the connection.
- [x] `ruff check` fails on a raw HTTP client outside the outbound module,
      proven by a test, and the exemption list is asserted to be exactly three
      paths.
- [x] `429` is retried at most three times; `401` is not retried at all.
- [x] `usage_quality=unavailable` fabricates no Token count and still enforces
      time and call-count limits.
- [ ] An AgentVersion with a Token limit cannot publish against an endpoint
      whose usage is unavailable — **not implemented**, see §5.4. The gap is
      pinned by a test.
- [x] `estimated` is rejected by the endpoint schema and by a check constraint.
- [x] Every existing deterministic AgentSpec still validates with an unchanged
      content hash.
- [x] No credential appears in any log, event payload, audit context, or API
      response.
- [x] No `workspace_id` column on `model_endpoints`; no `secrets` table; no
      streaming; no tool binding.
- [x] All phase 1, 2A, 2B, and 2C checks pass unchanged, including the restart
      drill.
- [ ] One manual run against a real vendor endpoint — **not performed**, see §6.
