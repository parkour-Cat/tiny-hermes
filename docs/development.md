# Development

## Requirements

- Python 3.12
- uv 0.11.26
- Node.js 24 LTS
- pnpm 10.15.0, invoked through Corepack
- Docker with Docker Compose

Check the installed versions from the repository root:

```powershell
python --version
uv --version
node --version
corepack pnpm --version
docker --version
docker compose version
```

Install the locked Python and JavaScript dependencies:

```powershell
$env:UV_CACHE_DIR = ".uv-cache"
uv sync --frozen
corepack pnpm install --frozen-lockfile
```

## First start

Create the local environment file and replace both example secrets with different random values of at least 32 characters:

```powershell
Copy-Item .env.example .env
```

`SESSION_COOKIE_SECRET` protects browser sessions. `BOOTSTRAP_TOKEN` is accepted only until the first platform administrator is created. Do not reuse either value outside this local installation.

Build the images, apply database migrations, and wait until the API and Web containers are healthy:

```powershell
docker compose --env-file .env -f deploy/compose/compose.yaml up -d --build --wait
docker compose --env-file .env -f deploy/compose/compose.yaml ps -a
```

Open `http://127.0.0.1:3000/bootstrap` to create the first administrator. The page sends the bootstrap token in a request header; it is never included in the URL. The equivalent API call is:

```powershell
$env:TINY_HERMES_BOOTSTRAP_TOKEN = "the BOOTSTRAP_TOKEN value from .env"
$bootstrapBody = @{
  subject = "admin@example.com"
  display_name = "Administrator"
  password = "replace-with-a-local-password"
} | ConvertTo-Json
try {
  Invoke-RestMethod `
    -Method Post `
    -Uri http://127.0.0.1:8000/api/v1/bootstrap `
    -ContentType "application/json" `
    -Headers @{ "X-Bootstrap-Token" = $env:TINY_HERMES_BOOTSTRAP_TOKEN } `
    -Body $bootstrapBody
} finally {
  Remove-Item Env:TINY_HERMES_BOOTSTRAP_TOKEN -ErrorAction SilentlyContinue
}
```

After a successful bootstrap, the same endpoint permanently returns `bootstrap_closed`. Sign in at `http://127.0.0.1:3000/login`.

## Local tests

Run the backend unit checks without external services:

```powershell
$env:UV_CACHE_DIR = ".uv-cache"
uv run --no-sync ruff check packages/backend migrations
uv run --no-sync pyright
uv run --no-sync pytest packages/backend/tests/unit -v
```

The Compose PostgreSQL initialization creates a separate `tiny_hermes_test` database. Apply migrations to that database before integration tests:

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test"
$env:TEST_DATABASE_URL = $env:DATABASE_URL
uv run --no-sync alembic upgrade head
uv run --no-sync pytest packages/backend/tests/integration -v
uv run --no-sync alembic check
Remove-Item Env:TEST_DATABASE_URL
Remove-Item Env:DATABASE_URL
```

Repeat the concurrency regressions before merging a change to Run Coordination or
Run Execution. CI runs the same six files ten times:

```powershell
1..10 | ForEach-Object {
  uv run --no-sync pytest `
    packages/backend/tests/integration/runs/test_run_creation.py `
    packages/backend/tests/integration/runs/test_run_control.py `
    packages/backend/tests/integration/runs/test_run_coordination.py `
    packages/backend/tests/integration/runs/test_run_retry.py `
    packages/backend/tests/integration/runs/test_scheduler.py `
    packages/backend/tests/integration/runs/test_execution_flow.py -q
}
```

Redis buys latency, never correctness, so the suite must also pass with no
reachable wake-up channel. Point `REDIS_URL` at a port nothing listens on and run
it again. The five tests in `test_wakeup.py` that assert the optimization itself
report as skipped; every other test must still pass, on polling alone:

```powershell
$env:REDIS_URL = "redis://127.0.0.1:6399/0"
uv run --no-sync pytest packages/backend/tests/integration -q -rs
Remove-Item Env:REDIS_URL
```

Run the Web checks and the browser acceptance tests. A setup project bootstraps
`admin@example.com`, signs in once, and shares the browser state with the rest; a
rerun against an already-opened platform is fine, but a platform bootstrapped
with some other account and password is not, because the setup cannot sign in and
cannot recover the password. Either follow the reset procedure below and rerun
the First start Compose command without bootstrapping by hand, or bring up a
second isolated stack with `-p tiny-hermes-e2e`, which leaves the first one's
volumes untouched.

```powershell
corepack pnpm web:lint
corepack pnpm web:test
corepack pnpm web:build
corepack pnpm exec playwright install chromium
$env:TINY_HERMES_E2E_BOOTSTRAP_TOKEN = "the BOOTSTRAP_TOKEN value from .env"
corepack pnpm exec playwright test --config tests/e2e/playwright.config.ts
Remove-Item Env:TINY_HERMES_E2E_BOOTSTRAP_TOKEN
```

## Agents, Sessions, and Runs

Phase 2A adds the Agent Catalog and Run Coordination management APIs. Every
request below needs the browser session cookie from `/api/v1/auth/sessions`,
the `X-CSRF-Token` header for writes, and `X-Workspace-Id` to select the
workspace. Sign in first and keep the cookie jar:

```powershell
$api = "http://127.0.0.1:8000/api/v1"
$login = Invoke-WebRequest -Uri "$api/auth/sessions" -Method Post `
  -ContentType "application/json" -SessionVariable browser `
  -Body (@{ subject = "admin@example.com"; password = "your local password" } | ConvertTo-Json)
$csrf = ($browser.Cookies.GetCookies("http://127.0.0.1:8000") |
  Where-Object Name -eq "tiny_hermes_csrf").Value
$workspaceId = "the workspace UUID from GET /api/v1/workspaces"
$headers = @{ "X-CSRF-Token" = $csrf; "X-Workspace-Id" = $workspaceId }
```

Create an Agent, replace its one Draft with a validated configuration, and
publish an immutable Agent Version:

```powershell
$agent = Invoke-RestMethod -Uri "$api/agents" -Method Post -WebSession $browser `
  -ContentType "application/json" -Headers $headers `
  -Body (@{ name = "Analyst"; alias = "analyst" } | ConvertTo-Json)

$spec = @{
  schema_version = 1
  personality = "You are a concise enterprise assistant."
  model_policy = @{ provider = "deterministic"; scenario = "complete" }
  tools = @()
  limits = @{
    max_execution_seconds = 900; max_elapsed_seconds = 86400
    max_model_calls = 20; max_tool_calls = 50; max_derived_retries = 3
  }
}
$draft = Invoke-RestMethod -Uri "$api/agents/$($agent.id)/draft" -Method Put `
  -WebSession $browser -ContentType "application/json" -Headers $headers `
  -Body (@{ expected_revision = 1; spec = $spec } | ConvertTo-Json -Depth 5)

Invoke-RestMethod -Uri "$api/agents/$($agent.id)/publish" -Method Post `
  -WebSession $browser -ContentType "application/json" -Headers $headers `
  -Body (@{ expected_revision = $draft.revision } | ConvertTo-Json)
```

Publishing an unchanged Draft returns `200` with the existing version instead of
allocating a new number. `POST /agents/{id}/rollback` points the Agent back at an
earlier version without rewriting history.

Create a Session and submit an idempotent Run. `Idempotency-Key` is required;
the first call returns `201` with `Location`, and a repeat of the same key and
body returns `200` with `Idempotent-Replayed: true`:

```powershell
$session = Invoke-RestMethod -Uri "$api/sessions" -Method Post -WebSession $browser `
  -ContentType "application/json" -Headers $headers `
  -Body (@{ agent_id = $agent.id } | ConvertTo-Json)

$runHeaders = $headers + @{ "Idempotency-Key" = [guid]::NewGuid().ToString() }
$run = Invoke-RestMethod -Uri "$api/runs" -Method Post -WebSession $browser `
  -ContentType "application/json" -Headers $runHeaders `
  -Body (@{ session_id = $session.id; input = "Summarize the weekly report." } | ConvertTo-Json)

Invoke-RestMethod -Uri "$api/runs/$($run.id)/pause" -Method Post -WebSession $browser `
  -ContentType "application/json" -Headers $headers `
  -Body (@{ expected_state_version = $run.state_version } | ConvertTo-Json)
```

`pause`, `resume`, `cancel`, and `retry` all require the state version or key
shown above, and every Run snapshot reports `queue`, `budget`, and
server-computed `available_actions`.

## Executing Runs

Compose starts a Worker and a Scheduler alongside the API, so a submitted Run now
reaches a terminal state on its own. Neither process accepts a request; work
arrives through PostgreSQL, and Redis only shortens the wait.

The Agent's published `model_policy.scenario` decides what its Runs do. All three
scenarios are answered by the deterministic provider, which makes no network
call:

These three are the deterministic provider's, and they remain selectable after
phase 3A. An Agent may instead name a real endpoint; see *Model endpoints*.

| Scenario | Behavior |
| --- | --- |
| `complete` | One model round, then `completed`. |
| `continue_once` | Two rounds, then `completed`. |
| `fail_replay_safe` | Fails at a replay-safe checkpoint, so `retry` is offered. |

Watch a Run as it executes. `GET /runs/{id}/events` is a Server-Sent Events
stream, and it selects the workspace with a `workspace_id` query parameter rather
than a header, because a browser `EventSource` cannot set one. Resume an
interrupted subscription with `Last-Event-ID`; the stream replays from that
sequence with no gap and no repeat, and closes itself once the Run is terminal.

`Invoke-RestMethod` buffers a whole response, so it cannot show a stream
arriving. Use `curl.exe`, and pass the session cookie the sign-in above already
placed in `$browser`:

```powershell
$sessionCookie = ($browser.Cookies.GetCookies("http://127.0.0.1:8000") |
  Where-Object Name -eq "tiny_hermes_session").Value
curl.exe -N -b "tiny_hermes_session=$sessionCookie" `
  "http://127.0.0.1:8000/api/v1/runs/$($run.id)/events?workspace_id=$workspaceId"
```

To run either process outside Compose, start it from the repository root in its
own shell. Both read `.env` the same way the API does, and its `DATABASE_URL` and
`REDIS_URL` already address the Compose services from the host, so no extra
configuration is needed. Both stop cleanly on Ctrl+C, finishing the slice in
flight:

```powershell
uv run --no-sync tiny-hermes-worker
```

```powershell
uv run --no-sync tiny-hermes-scheduler
```

Running one on the host while the Compose `worker` is also up is fine and is the
easiest way to watch two Workers compete for the same queue; the losing claim
simply finds nothing to do.

A Worker executes one Session at a time and returns the Run to `queued` at each
slice boundary, so a long Run is visibly claimed more than once. The Scheduler
expires the lease of a Worker that stopped answering and hands the Run back; that
recovery is what a killed Worker relies on, not any timeout inside the Worker.

**What phase 2B still did not have.** No real model provider, no tools, no file
handling, and no sandbox. Phase 3A adds the first of those; see *Model endpoints*
below. Everything in this section is provider-agnostic and unchanged by it.

## The console

Phase 2C puts the commands above behind pages, so publishing an Agent and
watching a Run no longer needs a shell. Sign in at
`http://127.0.0.1:3000/login`; everything below is under `/workspaces`.

| Route | Page |
| --- | --- |
| `/workspaces` | Every Workspace this account belongs to, and the form that creates one. |
| `/workspaces/:workspaceId/agents` | The Workspace's Agents, and the form that creates one. |
| `/workspaces/:workspaceId/agents/:agentId` | The one Draft, its revision, and publishing it as a version. |
| `/workspaces/:workspaceId/runs` | The Workspace's Runs, and the form that submits one. |
| `/workspaces/:workspaceId/runs/:runId` | One Run: its summary, its budget, its events as they arrive, and the actions the server says are available. |

The Workspace is a route parameter rather than a stored selection, so a reload or
a shared link reopens the same scope, and the console sends the `X-Workspace-Id`
the address implies. It never checks membership itself and never quietly
substitutes a Workspace it does know about: an address into a Workspace this
account has no standing in renders the server's `404`, which is the same answer a
Run that does not exist gets.

The Run list is not paginated. A `limit`/`cursor` contract that the console
pretends to honour while the server returns everything is a lie that gets found
in production, so the list shows what the server sends and nothing suggests
otherwise. Pagination arrives on the API first.

The Run detail page reads `GET /runs/{id}/events` with `fetch` rather than
`EventSource`, because `fetch` can send `X-Workspace-Id` and `Last-Event-ID` as
headers and can be aborted when the page unmounts. The `workspace_id` query
parameter stays supported for `EventSource` clients and has its own acceptance
test; nothing in the console relies on it.

**What phase 2C does not show.** Design §20.3 describes a Run Detail with a
parent/child task tree, context and compaction events, artifacts, and token and
cost accounting. None of those exist in the platform yet, so none of them appear
as an empty pane: a panel reading "no data" claims nothing happened, which is a
different statement from "not built yet". They arrive with the phases that
produce the data.

## Model endpoints

Phase 3A adds a second model provider: a real OpenAI-compatible endpoint. The
deterministic stand-in does not go away — an air-gapped installation still has to
be able to prove the platform works, and every test above the provider boundary
needs a Run whose outcome is known.

**A model endpoint is approved by a platform administrator, not by a workspace.**
`model_endpoints` is platform-scoped: a workspace administrator chooses among the
endpoints that exist and cannot register one.

**The platform stores no credential.** `credential_ref` names an environment
variable the deployment provides; the value is read when a call is made and
written nowhere — not to the database, not to a log, not to any response. This is
a real limitation with a real cost: a model key is deployment configuration
rather than workspace data, and rotating one is a restart. It is preferred to the
alternative available in this slice, because a table holding plaintext, or
ciphertext under a key with no rotation path, reads in a review as a control
while not being one. Secret storage with a rewrappable KEK is phase four.

Set the variable first, then register the endpoint. Registration refuses a
`credential_ref` the process does not define, so a broken configuration is found
by the administrator rather than inside somebody's Run:

```powershell
$env:TINY_HERMES_MODEL_KEY_ACME = "your endpoint key"
$endpoint = Invoke-RestMethod -Uri "$api/model-endpoints" -Method Post -WebSession $browser `
  -ContentType "application/json" -Headers @{ "X-CSRF-Token" = $csrf } `
  -Body (@{
    name = "acme-gpt"; kind = "openai_compatible"
    base_url = "https://models.example.com/v1"; model = "acme-large"
    context_window = 128000; max_output_tokens = 4096
    usage_quality = "provider"; credential_ref = "TINY_HERMES_MODEL_KEY_ACME"
  } | ConvertTo-Json)

Invoke-RestMethod -Uri "$api/model-endpoints/$($endpoint.id)/check" -Method Post `
  -WebSession $browser -Headers @{ "X-CSRF-Token" = $csrf }
```

The check makes one real request through the same guarded client a Run uses, and
answers with a verdict and a duration. It never reports the endpoint's status or
body: a `base_url` mistyped into an internal service would otherwise make that
route a way to read it.

`usage_quality` is the administrator's declaration of whether the endpoint
reports Token counts. `provider` means it does. `unavailable` means it does not,
and the platform then records `checkpoint_usage_quality=unavailable` on the Run
and adds nothing to `consumed_tokens` — because "nothing was used" and "nobody
counted" are different facts. Time limits and model-call counts are enforced
unchanged either way. `estimated` is rejected: technical design §9.4 admits an
estimate only from a tokenizer verified against the model, and none exists here.

Then select the endpoint in the draft editor's 模型提供方 field, or over the API:

```json
{ "provider": "openai_compatible", "endpoint_id": "the endpoint UUID" }
```

Publishing refuses an endpoint that does not exist or has been disabled, and an
`max_output_tokens` above the endpoint's own — refused rather than clamped,
because an Agent that quietly produces less than it was configured for behaves
unlike the one its author published. A draft may name anything; the check is at
publish, which is the last moment a mistake is still cheap.

### Outbound safety

Every model call and every endpoint check goes through `SafeOutboundClient`, and
nothing else in the process may open a connection — `ruff` fails the build on a
raw `httpx` client or socket outside `tiny_hermes/outbound/`.

It refuses loopback, link-local (which covers the AWS and GCP metadata address),
carrier-grade NAT (which covers Alibaba Cloud's), unique-local, private, and
reserved addresses. **Every** address a hostname resolves to is checked, not the
first, and the connection is then made to that literal address with the hostname
kept in the `Host` header and the TLS SNI — so a record that changes between the
check and the socket cannot be used. Every redirect hop starts over from
resolution, and a hop that crosses origin drops the `Authorization` header.

`OUTBOUND_ALLOWED_CIDRS` is how an enterprise private endpoint becomes reachable:

```powershell
$env:OUTBOUND_ALLOWED_CIDRS = "10.20.0.0/16"
```

Only private and carrier-grade NAT ranges can be opened this way. Loopback and
link-local stay refused however wide the approved range is, so approving
`0.0.0.0/0` to reach one internal endpoint does not open this host to itself. A
plaintext `http` endpoint is allowed only inside an approved range, because that
is the one place plaintext is an operator's deliberate choice about a network
they own.

**What phase 3A did not have.** No streaming — `stream` is not sent and nothing
consumes partial text yet; it arrives with Chat Completions in phase four. No
tools, sandbox, or file handling existed in that slice; phase 3B adds the first
tool and the sandbox below. There is still no Token limit: `AgentLimits` has no
such field, so §9.4's rule about strict Token ceilings has nothing to guard yet,
and adding the field means teaching the platform to read more than one
`schema_version`.

## Sandbox and `shell.exec`

Phase 3B adds one platform-owned Docker sandbox and one tool, `shell.exec`. The
container is a short-lived place for a Run to execute a command. It is **not** a
general Docker service, a long-lived development machine, or persistent file
storage.

Phase 3C adds the persistence the container itself deliberately lacks: a
Session's `/workspace/data` is checkpointed after every write-capable tool
round into immutable revisions in MinIO, restored into each fresh container
before its first model call, and governed by `file.list` / `file.read` /
`file.write` alongside `shell.exec`. Two facts are worth keeping straight:

- `WORKSPACE_MAX_BYTES` and the object limit are a **checkpoint quota** — what
  may become a committed revision — never a physical disk ceiling. A command
  can temporarily exceed them while it runs; the frozen post-command scan is
  what refuses to commit the excess and rolls that one step back.
- The stack now requires `S3_ACCESS_KEY` / `S3_SECRET_KEY` (the compose file
  defaults them for a local machine). The Controller deliberately runs with
  both **empty**: the one process holding the Docker socket holds no
  object-store credential, and CI asserts that in the running stack.

Build the approved runtime image, read its immutable digest, and pass that
digest to Compose. An empty `SANDBOX_IMAGE_DIGEST` approves no image and every
tool-bound Run fails closed rather than using a tag or running on the host:

```powershell
docker build -t tiny-hermes-sandbox:local -f deploy/sandbox/Dockerfile deploy/sandbox
$env:SANDBOX_IMAGE_DIGEST = (docker image inspect tiny-hermes-sandbox:local --format '{{.Id}}').Trim()
docker compose --env-file .env -f deploy/compose/compose.yaml up -d --build --wait
```

The Sandbox Controller is the only platform process that can talk to Docker. It
mounts `/var/run/docker.sock`; the API, Web, Worker, Scheduler, model, and sandbox
container do not. The Worker can only ask the Controller to perform its small,
fixed set of sandbox actions over a separate local socket. Reaching that socket
is not enough: the Controller checks that the Run owns the sandbox and still
holds a live Worker lease on every command. This matters because access to the
Docker socket is effectively full control of the host, so it belongs in the
smallest process rather than in request-handling or Agent code.

The Agent cannot choose an image, mount, host path, network mode, capability, or
resource profile. The Controller creates a non-root container with a read-only
root filesystem, no network, all Linux capabilities dropped, no privilege
escalation, and fixed CPU, memory, process, and temporary-space ceilings. Only
`/workspace/data`, `/workspace/cache`, and a limited `/tmp` are writable.

Phase 3B does **not** yet enforce the design's per-Run disk or file-count limit
on `/workspace/data` and `/workspace/cache`. Docker's writable-layer quota does
not cover these named volumes, so presenting that setting as active would be
misleading. This remains an open M1 requirement; do not treat the sandbox slice
as satisfying every resource ceiling until the named-volume limit and
`paused(limit)` behavior are implemented and tested.

Bind the tool in an Agent Draft before publishing:

```powershell
$spec.tools = @("shell.exec")
```

An Agent that does not bind `shell.exec` is not shown its schema, and a forged
call is still refused immediately before execution. The command is interpreted
by Bash. `cwd` must remain under `/workspace/data` or `/workspace/cache`, the
default timeout is 60 seconds, the maximum is 900 seconds, and output beyond
1 MiB is marked as truncated.

`cache_state` tells the runtime whether the same warm sandbox was reused. When
it is `reset`, the Run records `sandbox_cache_reset` and the next model call gets
a protected notice that `/workspace/cache` is empty; the Agent must not assume a
previous install or background process still exists. `reused` means the same
Run's warm sandbox was recovered for another execution slice. A different Run
never shares its writable layer.

`/workspace/data` can survive a sandbox replacement while the **same Run** is
being recovered, but phase 3B does not commit it to a Session workspace or an
artifact store. A later Run must treat it as absent. Do not put user-visible
claims of persistence around it yet.

## Restart drill

The Worker, the Scheduler, and Redis are separate processes, and the claim that a
committed Run survives losing any one of them is only worth what it has been
tested against. `scripts/restart_drill.py` restarts them under load against a
running stack and checks what came out the other side.

The drill needs a Run long enough for a restart to land inside it and an approved
sandbox image for its fourth scenario. Bring the stack up with both values and
pass them to the script; below one second it refuses to run rather than prove
nothing:

```powershell
$env:DETERMINISTIC_MODEL_DELAY_MS = "3000"
$env:SANDBOX_IMAGE_DIGEST = (docker image inspect tiny-hermes-sandbox:local --format '{{.Id}}').Trim()
docker compose --env-file .env -f deploy/compose/compose.yaml up -d --wait
uv run --no-sync python scripts/restart_drill.py
Remove-Item Env:DETERMINISTIC_MODEL_DELAY_MS
Remove-Item Env:SANDBOX_IMAGE_DIGEST
```

There is a second drill since phase 3C. `scripts/workspace_drill.py` proves the
session workspace through the public API: a committed write survives killing
the Worker, two Sessions cannot see each other's files, an over-quota command
pauses the Run honestly and resume finds the preceding revision, and nothing
labelled `tiny-hermes.run` — container or volume — outlives the drill. Run it
twice, once as-is and once with `WORKSPACE_MAX_BYTES=8388608` and
`--phase quota` for the quota scenario; CI does exactly that in `compose-e2e`.

The restart drill signs in as the local administrator, publishes its own Agents, and runs four
scenarios: the Worker restarted mid-slice, Redis stopped and started again, and
the Worker killed while holding a lease with the Scheduler restarted underneath
it; finally it waits until `shell.exec` is visibly running inside a live sandbox,
kills the Worker, and requires the Run to pass through `interrupted`, recover,
complete, and leave no sandbox container behind. Each one has to end `completed`
with an event history numbered from one with nothing skipped and nothing
repeated. It prints identifiers, statuses, sequence counts, and timings — never
a cookie, a token, a password, or a Run's input.

The drill restarts containers. It never takes the stack down and never removes a
volume, and it refuses `down`, `rm`, `-v`, and `--volumes` outright rather than
trusting itself to stay careful. Recreating the stack for the slower model keeps
the volumes, so the account you bootstrapped is still there afterwards. Set
`DETERMINISTIC_MODEL_DELAY_MS` back, or drop it, when you are done.

Two of its timings are worth reading rather than only passing. The Worker restart
takes about thirty seconds to resolve, because a Worker asked to stop finishes
its slice first. The first Run submitted after Redis returns is usually picked up
a whole poll interval late, while the subscription is being re-established; a
wake-up is published once and never repeated, so missing one costs exactly that
and nothing more. The drill submits several and asks only that the channel
delivers again.

## Database migrations

Start PostgreSQL, set `DATABASE_URL` to the local test database, and bring it to the current revision before generating a migration:

```powershell
docker compose --env-file .env -f deploy/compose/compose.yaml up -d postgres
$env:DATABASE_URL = "postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test"
uv run --no-sync alembic upgrade head
uv run --no-sync alembic revision --autogenerate -m "describe_change"
```

Review every generated operation before applying it. An automatically generated file is a draft, not proof that data will remain safe. Then check the upgrade and downgrade path against the disposable test database:

```powershell
uv run --no-sync alembic upgrade head
uv run --no-sync alembic check
uv run --no-sync alembic downgrade base
uv run --no-sync alembic upgrade head
Remove-Item Env:DATABASE_URL
```

Never run the downgrade smoke test against a database containing data that must be kept.

## Reset local development data

First confirm the Compose project name and the exact named volumes:

```powershell
docker compose --env-file .env -f deploy/compose/compose.yaml config --volumes
docker volume ls --filter label=com.docker.compose.project=tiny-hermes
```

For this repository the expected named volumes are `tiny-hermes_postgres-data` and `tiny-hermes_minio-data`. If any other project or volume appears, stop and investigate before continuing.

After confirming the targets, remove only this Compose project's containers, network, and named volumes:

```powershell
docker compose --env-file .env -f deploy/compose/compose.yaml down -v
```

This permanently deletes the local PostgreSQL and MinIO data in those two volumes. Redis is configured without a persistent volume.

## Security notes

- Never commit `.env`, browser cookies, passwords, bootstrap tokens, database dumps, or real service credentials.
- The values in Compose are for an isolated local machine only. An enterprise deployment must use generated secrets and protected secret delivery.
- The first successful bootstrap closes the bootstrap endpoint permanently; changing the token does not reopen it.
- Local PostgreSQL and MinIO passwords are deliberately development-only and must not be copied into a production manifest.
- Environment variables can be visible through process and container inspection. Future KEK support will use protected file mounts or an external key manager instead of ordinary environment variables.
- M1 phase 3B provides Agent publication, Run execution, model endpoints, and the
  platform-owned `shell.exec` tool inside a short-lived Docker sandbox. Approval,
  secret management, Session-level file persistence, and artifact delivery do not
  exist yet; no tool may execute on the API, Worker, or host directly.
- Agent personality text is never echoed in an error response, and a resource in another workspace always answers with a generic `404`.
- A wake-up message carries a workspace ID and a Run ID and nothing else. Redis never sees a Run's input, an Agent's personality, or any other content.
- The event stream selects its workspace through a query parameter because `EventSource` cannot send a header. Authorization is still the session cookie, and a Run in another workspace answers `404` whatever the parameter says.
- Model endpoint credentials are deployment environment variables, never database rows. The platform stores the variable's name and reads its value at call time; no route returns either the value or the name. Rotating a key is a restart until phase four adds Secret storage.
- Everything that leaves the process goes through `tiny_hermes.outbound`, and `ruff` fails the build on a raw HTTP client or socket built anywhere else. The check is not advisory.
- A refused outbound address is reported to a workspace member as a code only. The resolved address goes to the audit trail, because a refusal that names an internal IP is a way to map the network the platform runs on.
- The endpoint connectivity check reports a verdict and a duration, never the endpoint's status or body. A `base_url` mistyped into an internal service would otherwise turn that route into a way to read it.
