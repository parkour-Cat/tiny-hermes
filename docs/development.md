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

Repeat the concurrency regressions before merging a change to Run Coordination.
CI runs the same four files ten times:

```powershell
1..10 | ForEach-Object {
  uv run --no-sync pytest `
    packages/backend/tests/integration/runs/test_run_creation.py `
    packages/backend/tests/integration/runs/test_run_control.py `
    packages/backend/tests/integration/runs/test_run_coordination.py `
    packages/backend/tests/integration/runs/test_run_retry.py -q
}
```

Run the Web checks and the browser acceptance test. The browser test creates the first administrator, so it requires empty local development volumes. If this platform has already been bootstrapped, follow the reset procedure below, then rerun the First start Compose command but do not bootstrap manually.

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

**Runs stay `queued` in phase 2A.** There is no Worker process, no Scheduler,
no Redis wake-up, no SSE stream, no model call, and no sandbox yet; those arrive
in phase 2B, and the minimum Agent Builder and Run Detail pages arrive in phase
2C. A queued Run that never starts is the correct observable behavior today, not
a defect.

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
- The M1 foundation provides Agent publication and Run acceptance rules only. Approval, secret management, tools, files, and sandbox features do not exist yet.
- Agent personality text is never echoed in an error response, and a resource in another workspace always answers with a generic `404`.
