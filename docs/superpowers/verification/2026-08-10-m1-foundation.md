# M1 foundation verification — 2026-08-10

## Scope and revision

- Branch: `codex/m1-foundation`
- Verified implementation commit: `4c3b949469989ca72aad142cacb7f55d1d9a612a`
- Scope: M1 phase-one foundation through Task 9
- Environment: Windows host with Linux containers

This record contains command outcomes and redacted configuration facts only. It does not contain browser cookies, passwords, bootstrap tokens, database connection strings, or service credentials.

## Tool versions

| Tool | Verified version |
| --- | --- |
| Python | 3.12.6 |
| uv | 0.11.26 |
| Node.js | 24.6.0 |
| pnpm | 10.15.0 |
| Docker Engine | 29.5.3 |
| Docker Compose | 5.1.4 |
| Playwright | 1.62.1 |
| actionlint | 1.7.12 |

## Verification results

### Locked dependency installation

Commands:

```powershell
$env:UV_CACHE_DIR = ".uv-cache"
uv sync --frozen
corepack pnpm install --frozen-lockfile
```

Result: passed. Python checked 47 locked packages; pnpm reported the lockfile was current.

### Backend unit and static checks

Commands:

```powershell
uv run --no-sync ruff check packages/backend migrations
uv run --no-sync pyright
uv run --no-sync pytest packages/backend/tests/unit -v
```

Result: passed with 0 lint errors, 0 type errors, and 16 unit tests passed.

### PostgreSQL integration and migration round trip

The database variables were set to the disposable Compose test database without recording their values here.

Commands:

```powershell
uv run --no-sync alembic upgrade head
uv run --no-sync pytest packages/backend/tests/integration -v
uv run --no-sync alembic check
uv run --no-sync alembic downgrade base
uv run --no-sync alembic upgrade head
```

Result: passed with 8 integration tests. Alembic reported no ungenerated operations, and the downgrade/upgrade round trip exited successfully.

The integration suite includes the following required evidence:

- two simultaneous bootstrap attempts commit exactly one platform administrator;
- login, current-user lookup, CSRF enforcement, and logout work through the real API;
- cross-workspace access is rejected without exposing the protected workspace name;
- database unavailable, reachable empty schema, and migration-behind readiness cases are distinguished;
- the initial migration creates the required identity, tenancy, and audit tables and safety columns.

### Web checks

Commands:

```powershell
corepack pnpm web:lint
corepack pnpm web:test
corepack pnpm web:build
```

Result: passed with 1 Vitest flow test and a successful production build. The largest generated JavaScript file was 349.29 kB before gzip; Vite emitted no chunk-size warning.

### CI workflow syntax

The workflow file was mounted read-only into actionlint 1.7.12.

Result: passed with no findings. The workflow keeps four independent jobs: backend unit, backend integration, Web, and Compose end-to-end.

### Clean Compose and browser acceptance

Before reset, `docker compose ... config --volumes` and the Compose project label both resolved exactly these named volumes:

- `tiny-hermes_postgres-data`
- `tiny-hermes_minio-data`

Only those disposable local volumes were removed. They are not recoverable.

Commands:

```powershell
docker compose -f deploy/compose/compose.yaml up -d --build --wait
corepack pnpm exec playwright test --config tests/e2e/playwright.config.ts
docker compose -f deploy/compose/compose.yaml ps -a
```

Result: passed from empty volumes. The browser created the first administrator, signed in, created two workspaces, reloaded and observed both persisted records, signed out, and generated `playwright-report/index.html`.

Final Compose state:

- PostgreSQL, Redis, MinIO, API, and Web: healthy;
- one-time migration container: exited with code 0.

The same browser flow was also run once with a custom local bootstrap token supplied through an explicit root `.env` file and passed after the documented `--env-file .env` startup command.

### Sensitive-content and workspace checks

The plan's broad `sk-` expression produced two false positives from the prose `task-by-task`. A second scan limited to tracked files and secret-shaped minimum lengths found no private-key header, long `sk-` token, or AWS access-key shape.

Additional checks:

```powershell
git diff --check
git check-ignore -v .env
```

Result: passed. The local `.env` is ignored by Git, and `.dockerignore` excludes `.env` files from Docker build contexts.

## Known limits after phase one

This is a verified platform foundation, not yet the complete M1 agent runtime. It does not yet ship:

- Agent drafts or immutable Agent versions;
- Run, Session, Worker, or Scheduler execution;
- model-provider calls or the Hermes-style goal loop;
- sandbox execution or sandbox leases;
- ServiceAccounts, API keys, secret management, approval queues, or outbound tools;
- child-Agent orchestration, memory, skills, Feishu delivery, or end-user Web Chat.

These items remain in later implementation phases and must not be inferred from the passing foundation checks.
