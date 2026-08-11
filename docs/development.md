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

**What phase 2B still does not have.** There is no real model provider, no tools,
no file handling, and no sandbox — the deterministic provider is the only one that
exists, and a Run's `input` is never sent anywhere.

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

## Restart drill

The Worker, the Scheduler, and Redis are separate processes, and the claim that a
committed Run survives losing any one of them is only worth what it has been
tested against. `scripts/restart_drill.py` restarts them under load against a
running stack and checks what came out the other side.

The drill needs a Run long enough for a restart to land inside it, so bring the
stack up with a slower deterministic model and pass the same value to the script;
below one second it refuses to run rather than prove nothing:

```powershell
$env:DETERMINISTIC_MODEL_DELAY_MS = "3000"
docker compose --env-file .env -f deploy/compose/compose.yaml up -d --wait
uv run --no-sync python scripts/restart_drill.py
Remove-Item Env:DETERMINISTIC_MODEL_DELAY_MS
```

It signs in as the local administrator, publishes its own Agent, and runs three
scenarios: the Worker restarted mid-slice, Redis stopped and started again, and
the Worker killed while holding a lease with the Scheduler restarted underneath
it. Each one has to end `completed` with an event history numbered from one with
nothing skipped and nothing repeated. It prints identifiers, statuses, sequence
counts, and timings — never a cookie, a token, a password, or a Run's input.

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
- M1 provides Agent publication, Run acceptance, and deterministic Run execution only. Approval, secret management, tools, files, and sandbox features do not exist yet.
- Agent personality text is never echoed in an error response, and a resource in another workspace always answers with a generic `404`.
- A wake-up message carries a workspace ID and a Run ID and nothing else. Redis never sees a Run's input, an Agent's personality, or any other content.
- The event stream selects its workspace through a query parameter because `EventSource` cannot send a header. Authorization is still the session cookie, and a Run in another workspace answers `404` whatever the parameter says.
