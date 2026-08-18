# Development

## Requirements

- Python 3.12
- uv 0.11.26
- Node.js 24 LTS
- pnpm 10.15.0, invoked through Corepack
- Docker with Docker Compose

The command blocks in this file are PowerShell, because that is the shape of
the machine most of this was written on. *Installing on Linux* below is the
same sequence in bash; the API calls further down translate to `curl` without
changing a single header or body.

Check the installed versions from the repository root. On Linux the binary is
`python3`, and a distribution that ships something other than 3.12 is not a
problem: `uv sync` fetches the version this project pins, whatever the system
Python is.

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

## Installing on Linux

A fresh Ubuntu host has none of the five requirements. This is the whole
sequence, verified on Ubuntu 26.04 with 8 vCPU and 16 GB
(`docs/superpowers/verification/2026-08-16-fresh-host-install.md`).

Ubuntu's `needrestart` opens a dialog after a libc upgrade. Over SSH, with no
terminal for it to draw on, `apt-get` is stopped by `SIGTTOU` and waits
forever rather than failing — so silence both frontends before installing
anything:

```bash
sudo mkdir -p /etc/needrestart/conf.d
printf '%s\n' '$nrconf{restart} = "a";' '$nrconf{kernelhints} = -1;' \
  | sudo tee /etc/needrestart/conf.d/99-noninteractive.conf
export DEBIAN_FRONTEND=noninteractive
```

Docker and Compose come from the distribution. Ubuntu 26.04 carries Docker
29.1.3 and Compose 2.40.3, which is what CI uses:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"   # log out and back in, or the socket is refused
```

uv is pinned to the version in *Requirements*:

```bash
curl -LsSf https://astral.sh/uv/0.11.26/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

Node 24 is **not** in Ubuntu's archive — 26.04 offers 22.x — so take the
official tarball and let Corepack place pnpm:

```bash
mkdir -p ~/.local/node
curl -fsSL https://nodejs.org/dist/v24.6.0/node-v24.6.0-linux-x64.tar.xz \
  | tar -xJ --strip-components=1 -C ~/.local/node
export PATH="$HOME/.local/node/bin:$PATH"
corepack enable --install-directory "$HOME/.local/node/bin"
corepack prepare pnpm@10.15.0 --activate
```

Then the repository and its locked dependencies. The clone needs no
credential — the repository is public. A host that should hold none of its
own anyway can be handed the tree over `git bundle` instead, which is how the
fresh-host verification was done:

```bash
git clone https://github.com/parkour-Cat/tiny-hermes.git
cd tiny-hermes
export UV_CACHE_DIR=".uv-cache"
uv sync --frozen
corepack pnpm install --frozen-lockfile
```

Both `PATH` lines are for this shell only. Put them in `~/.profile` if this
host is going to be used more than once.

## First start

Create the local environment file and mint `SESSION_COOKIE_SECRET`,
`BOOTSTRAP_TOKEN`, and `TINY_HERMES_KEK` (32-byte standard base64). Do not keep
the documented Compose zeroes on a machine that will hold data:

```powershell
uv run --no-sync python scripts/generate_local_secrets.py --env-file .env
```

If `.env` already exists, pass `--force` to replace only those secret keys.
`SESSION_COOKIE_SECRET` protects browser sessions. `BOOTSTRAP_TOKEN` is accepted only until the first platform administrator is created. Do not reuse either value outside this local installation.

Build the sandbox runtime image and approve it **by digest**. An empty
`SANDBOX_IMAGE_DIGEST` approves nothing, so every tool-bound Run fails closed
rather than falling back to a tag or to the host — which means an Agent that
binds `shell.exec` or the `file.*` tools cannot run at all until this is set.
*Sandbox and `shell.exec`* explains what the image is; this is where it has to
happen:

```powershell
docker build -t tiny-hermes-sandbox:local -f deploy/sandbox/Dockerfile deploy/sandbox
$env:SANDBOX_IMAGE_DIGEST = (docker image inspect tiny-hermes-sandbox:local --format '{{.Id}}').Trim()
```

```bash
docker build -t tiny-hermes-sandbox:local -f deploy/sandbox/Dockerfile deploy/sandbox
export SANDBOX_IMAGE_DIGEST="$(docker image inspect tiny-hermes-sandbox:local --format '{{.Id}}')"
```

Build the images, apply database migrations, and wait until the API and Web containers are healthy:

```powershell
docker compose --env-file .env -f deploy/compose/compose.yaml up -d --build --wait
docker compose --env-file .env -f deploy/compose/compose.yaml ps -a
```

The same two lines are shell-agnostic; on Linux they run unchanged. Nine
services report healthy — `postgres`, `redis`, `minio`, `api`, `web`,
`worker`, `scheduler`, `controller`, and `migrate` having exited 0.

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

In bash, with the token read straight out of `.env` so it never reaches the
shell history:

```bash
TOKEN=$(grep '^BOOTSTRAP_TOKEN=' .env | cut -d= -f2-)
curl -s -X POST http://127.0.0.1:8000/api/v1/bootstrap \
  -H "X-Bootstrap-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"subject":"admin@example.com","display_name":"Administrator",
       "password":"replace-with-a-local-password"}'
unset TOKEN
```

After a successful bootstrap, the same endpoint permanently returns `bootstrap_closed`. Sign in at `http://127.0.0.1:3000/login`.

## Operator walkthrough

On a fresh Linux Docker host, after *First start* and sign-in, a reviewer can
prove the M1 console and machine-identity path without inventing a second
protocol. The PowerShell blocks later in this file are the same calls.

The pages below are served on the host's own loopback. If that host is a
remote machine reached over SSH, forward the two ports and open the URLs on
the workstation — nothing in the platform listens on a public interface, and
it should stay that way:

```bash
ssh -N -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@host
```

Every step below can also be driven entirely over the API with `curl`; the
walk in `docs/superpowers/verification/2026-08-16-fresh-host-install.md` is
exactly that, in order, and is what a headless reviewer should copy.

1. Open `http://127.0.0.1:3000/workspaces` and create **two** workspaces.
2. Inside the first workspace, create an Agent (name `Analyst`, alias `analyst`).
   On the Agent page bind the tools the walk needs — `shell.exec` and
   `file.list` are enough, and *First start* has already approved the image
   they run in — enable Chat Completions delivery (`enabled`,
   `sync_timeout_seconds` 60), and publish. The field-level diff is against the
   published spec; rollback is on the same page.
3. Open Playground from that Agent. Send a message. Playground posts
   `POST /api/v1/runs` with a cookie, CSRF, and a fresh `Idempotency-Key`; it is
   not Chat Completions. The Run Detail page lists transcript, tools, and files.
4. Under **API Keys**, mint a ServiceAccount (`developer`) and an API Key with
   `runs.read`, `runs.write`, and `runs.control`. The plaintext `token` appears
   once. Listing afterwards shows only `prefix`.
5. Call Completions with that token. `model` is the Agent alias. The route is
   `POST /v1/chat/completions` (no `/api` prefix) and requires `Idempotency-Key`:

```powershell
$completionHeaders = @{
  Authorization = "Bearer $token"
  "Idempotency-Key" = [guid]::NewGuid().ToString()
  "Content-Type" = "application/json"
}
$body = @{
  model = "analyst"
  messages = @(@{ role = "user"; content = "Summarize the weekly report." })
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/chat/completions" `
  -Method Post -Headers $completionHeaders -Body $body
```

A second default Completions request without `X-Tiny-Hermes-Session-Id` is a
new ephemeral Session. A blocked persistent Session returns 409
`session_blocked` and inserts no Run. Secrets, members, and model endpoints
are in the header nav; there is no Approvals page.

The §24.1 benchmark is `uv run --no-sync python scripts/benchmark_m1.py`. It
refuses to report a pass unless the host is Linux with at least 8 vCPU and a
MemTotal of 15 GiB, which is what a 16 GB machine reports once the kernel and
the hypervisor take their pages. When `/health/ready` is 200 it runs the ten
live drivers in
`scripts/benchmark_live.py`. `--seconds` may shorten a duration gate's sample;
a shorter sample cannot pass. Do not edit its thresholds to make a run green.

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
server-computed `available_actions`. When the Session's head is `paused` or
`waiting_*`, a second Runs API submit still returns `201` and a
`queue.status` of `session_blocked` that names the head, its reason, and the
actions this caller may take on **that head**.

## Machine identity

A workspace administrator mints a ServiceAccount (developer or viewer, never
admin) and then an API Key. The plaintext token is in the create response
once, as `token`, and is never stored or listed afterwards. Listing returns
`prefix` and scopes. The authenticated caller on Sessions and Runs is the
ServiceAccount; the key's scopes are intersected with the account's role.

```powershell
$account = Invoke-RestMethod -Uri "$api/service-accounts" -Method Post `
  -WebSession $browser -ContentType "application/json" -Headers $headers `
  -Body (@{ name = "ci-runner"; role = "developer" } | ConvertTo-Json)

$issued = Invoke-RestMethod `
  -Uri "$api/service-accounts/$($account.id)/api-keys" -Method Post `
  -WebSession $browser -ContentType "application/json" -Headers $headers `
  -Body (@{ scopes = @("runs.read", "runs.write", "runs.control") } | ConvertTo-Json)

# $issued.token is shown once. Keep it out of logs and out of git.
$token = $issued.token
```

`GET /api/v1/service-accounts/{id}/api-keys` never includes `token`.
`POST /api/v1/api-keys/{id}/revoke` disables that key. A Bearer request that
sends a disagreeing `X-Workspace-Id` is the same generic 403 a missing
membership gets. Completions and the Runs API both use
`Authorization: Bearer <token>`; they do not accept the browser session cookie
as a substitute on `POST /v1/chat/completions`.

## Chat Completions

Enable delivery on the Agent spec before publishing (`delivery.enabled = true`,
`sync_timeout_seconds` 1–60, default 60). The default omitted block does not
change published content hashes. `model` is the Agent's workspace alias.

`POST /v1/chat/completions` sits at the API root, not under `/api`. It requires
`Idempotency-Key`. With no session header the platform creates an ephemeral
Session. `X-Tiny-Hermes-Session-Id` must name a persistent Session this caller
already owns for this Agent; a blocked persistent Session returns 409
`session_blocked` and inserts no Run.

```powershell
$spec.delivery = @{ enabled = $true; sync_timeout_seconds = 60 }
# republish the draft as above, then:

$completionHeaders = @{
  Authorization = "Bearer $token"
  "Idempotency-Key" = [guid]::NewGuid().ToString()
  "Content-Type" = "application/json"
}
$body = @{
  model = "analyst"
  messages = @(@{ role = "user"; content = "Summarize the weekly report." })
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/chat/completions" `
  -Method Post -Headers $completionHeaders -Body $body
```

For a stream, set `stream = $true` and use `curl.exe -N`. After the response
headers, a pause or timeout is an SSE `event: error` frame, not a JSON 409.
Token chunks are not stored as RunEvents.

A Completions Run holds its Worker lease until it finishes or
`sync_timeout_seconds` elapses (`paused(compat_timeout)`). Other pauses tell
the client `requires_runs_api`. The Scheduler cancels a `compat_timeout` pause
that is still there after 24 hours.

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
| `/workspaces/:workspaceId/agents/:agentId` | Draft editor: tools, Chat Completions delivery, field-level diff, rollback, rename. |
| `/workspaces/:workspaceId/agents/:agentId/playground` | Persistent Session over the Runs API (cookie + CSRF). Not Chat Completions. |
| `/workspaces/:workspaceId/runs` | The Workspace's Runs, and the form that submits one. |
| `/workspaces/:workspaceId/runs/:runId` | One Run: summary, budget, transcript, tools, files, folded event payloads, and the actions the server offers. |
| `/workspaces/:workspaceId/members` | Invite an existing user by email; change role; remove. Unknown email is an error, not a signup. |
| `/workspaces/:workspaceId/api-keys` | Service accounts and API Keys. Plaintext is shown once. |
| `/workspaces/:workspaceId/model-endpoints` | Selectable endpoints. Platform administrators also register, check, enable, and disable; detail shows `base_url` and `credential_available`, never the credential. |
| `/workspaces/:workspaceId/secrets` | Create a Secret (plaintext is typed here only). The list shows name, scope, mask, and status. Platform administrators can rewrap. |

The header carries a language switcher (default `zh-CN`, persisted as
`tiny-hermes-locale`) and a light/dark toggle (persisted as `tiny-hermes-theme`).
Playground is reached from an Agent, not from a top-level nav item. There is no
Approvals, Usage, task-tree, skills, or Feishu page.

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

**What this console still does not show.** Parent/child task trees, context and
compaction events, and token/cost accounting remain M2/M3. Files (产物) on Run
Detail and Playground list Artifacts the platform already stored.

## Playground

Playground is a Runs API client with a cookie, never a Chat Completions client.
Opening `/workspaces/:workspaceId/agents/:agentId/playground` reuses the latest
persistent Session for this Agent and this signed-in user, or creates one.
Each send posts `POST /api/v1/runs` with a fresh `Idempotency-Key`. When the
head Run is paused or waiting, a further send still returns `201` and the page
shows the queue snapshot, including the head's `available_actions`. 「新 Session」
opens an unrelated persistent Session.

Files download through `GET /api/v1/artifacts/{id}/content` with
`X-Workspace-Id`; a bare link cannot send that header.

Members, API Keys, Secrets, and model endpoints are listed in the header nav of a
workspace. Inviting a member requires an email that already belongs to a User.
An API Key's plaintext appears once in a dismissible panel; later listings show
only the prefix. A Secret's plaintext is typed on create and is never returned
by GET; the list shows a mask.

## Outbound and the egress proxy

Nothing in this platform reaches the network on its own. Every outbound
request — model calls, skill imports from Git, endpoint connectivity checks,
and anything a sandbox does — goes through a separate process whose only job is
to decide whether a packet may leave. A deployment that has not stood one up
sends **nothing**; there is no code path that falls back to a direct
connection, which is what makes that sentence true rather than a rule people
follow.

Bring it up with the rest of the stack and give the platform processes its
address and a token:

```powershell
$env:EGRESS_PROXY_URL = "http://egress-proxy:3128"
$env:EGRESS_PROXY_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))")
$env:SANDBOX_EGRESS_NETWORK = "tiny-hermes_sandbox-egress"
docker compose --env-file .env -f deploy/compose/compose.yaml up -d --build
```

The token answers "is this one of ours" and nothing more: what a caller may
reach still comes from the scope tables, so a leaked token widens nothing by
itself. A sandbox presents no token at all — it is identified by the address
its packets come from, because a process inside a container that holds a
credential is a process that can lend one.

### Four layers, and none of them may widen

The effective scope of any request is the intersection of four:

| Layer | Who sets it | Where |
|---|---|---|
| Platform | Platform administrator | 出站范围 → 平台批准 |
| Workspace | Workspace administrator, inside the platform's | 出站范围 → 本工作空间 |
| Agent | Agent author, inside the workspace's | Agent builder → 网络 |
| Run | Delegation (M2E) | not yet |

An entry is a host (`api.example.com`), one leftmost wildcard
(`*.example.com`), or a network (`10.1.0.0/16`). Never a URL, never a port: a
scope approves a target, and the port belongs to the request. `*` and `*.com`
are refused — an entry nobody can review at a glance is an entry nobody
reviews.

Everything starts empty. A workspace naming something the platform never
approved is refused when it is written, and an Agent naming something its
workspace did not approve is refused at publish with every offending entry
listed. A layer can only ever narrow the one above it.

Registering a model endpoint approves the host it names, and disabling the
endpoint takes the approval away. That entry is marked as the endpoint's and
cannot be removed by hand: choosing an endpoint *is* the approval, and a second
step would be one that gets forgotten rather than a judgement that gets made.

### What a sandbox can reach

With `SANDBOX_EGRESS_NETWORK` set, a sandbox is attached to a Docker network
declared `internal` — a bridge with no gateway. The only thing on it besides
sandboxes is the proxy, so a container has exactly one place to send a packet
and that place decides. Without the setting a sandbox has **no network at
all**, which is the default and what §16.4 asks for.

The container also gets `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`, for the
runtimes that read them. Nothing relies on that: a tool that ignored them would
still find only one reachable address.

A sandbox holds its identity only while it may use it. Freezing an instance
removes it, thawing restores it, and destroying removes it before the container
goes — otherwise Docker could hand the address to the next container and make
it this Run.

### Reading a refusal

A refusal from the boundary is not the target saying no, and the two are told
apart on purpose: one will never change on a retry. A refused plaintext request
carries its reason back (`target_not_in_scope`, `scope_empty`,
`plaintext_not_approved`, and the address classes such as `link_local`). A
refused TLS target is reached through `CONNECT`, so there is no response for a
reason to ride on and the caller sees `egress_unavailable` — the specific rule
is in the proxy's log.

## Skills

A skill is reference material an administrator uploads. Binding one gives an
Agent **no** new capability: no tool, no network, no credential. It changes
what the Agent is told, not what it may do.

Upload one from 技能 in the workspace nav. The picker takes a **directory of
files**, never an archive — the browser reads them and posts a list, so no
server here unpacks anything. The directory needs a `SKILL.md` at its root with
two frontmatter fields:

```markdown
---
name: rollout
description: How this company takes a machine out of rotation before a deploy.
---

# Rollout

Take the machine out of the pool first, then drain it.
```

The name is read from the file rather than typed into a form, so the catalog
and the package cannot disagree about what a skill is. A version whose files
carry credential material is refused outright, with the offending path named.
「从 Git 导入」 takes a public HTTPS `tar.gz` URL instead; it goes through the
platform's outbound policy like any other external request, is read as a stream,
and is never written to disk. Re-importing unchanged content answers 200 and
creates no second version.

### What a bound Agent actually sees

Bind a skill in the Agent builder's 技能 section. What is stored is a **skill
version id**, never a name. Every round then carries one line per bound skill —
its name and its description — inside boundary markers, as a system message of
its own after the personality:

```
--- begin skills available to you (workspace material) ---
- rollout: How this company takes a machine out of rotation before a deploy.
--- end skills available to you ---
```

The document itself costs nothing until the model asks for it. An Agent that
also binds `skill.load` may call it with a skill name (and optionally a path
inside the package, `SKILL.md` by default); the file comes back as a tool
result and the Run's timeline gains a `skill_loaded` entry saying which
document entered the conversation. One load may bring in at most 64 KiB — a
larger file is refused *with its size*, never truncated — and one Run may load
at most eight times.

When the bound summaries no longer fit their segment of the context budget, the
platform drops whole summaries, newest binding first, and never a partial one.
A skill this Run has already loaded is never dropped. Publishing refuses
outright if the summaries cannot fit at all, and the refusal says what each one
costs so the expensive description can be found.

### Why publishing a new skill version changes nothing

Because an Agent binds a version id. Uploading version 2 of `rollout` leaves
every published Agent running version 1, including the ones that were running
when you uploaded it. Moving 「设为新绑定起点」 changes where the *next* binding
starts and nothing else; 停用 stops new bindings and leaves running ones alone.
Switching an Agent over is a deliberate act: edit its draft to name the new
version, and publish again.

### Proposals

An Agent that binds `skill.propose` may suggest a change to a skill it was
given, or a new skill. What it writes is a **pending proposal** and never a
version — there is no path from a proposal to a version that does not pass
through a person. One proposal per Run.

Review them under 技能提案: the queue shows where each came from, with a link
to the Run that opened it, and 「差异」 shows the change line by line against
the version the Agent actually read. Approving publishes a new immutable
version whose source is recorded as the proposal; rejecting ends it and creates
nothing. Both are audited. A proposal the static scan blocked can be read and
cannot be approved, and the console says which file stopped it.

Approving does **not** repoint anything. The skill's default for new bindings
stays where it was, and so does every published Agent's binding.

## Model endpoints

Phase 3A adds a second model provider: a real OpenAI-compatible endpoint. The
deterministic stand-in does not go away — an air-gapped installation still has to
be able to prove the platform works, and every test above the provider boundary
needs a Run whose outcome is known.

**A model endpoint is approved by a platform administrator, not by a workspace.**
`model_endpoints` is platform-scoped: a workspace administrator chooses among the
endpoints that exist and cannot register one.

**The platform stores no credential on the endpoint.** `credential_ref` names
either an environment variable the deployment provides, or the id of an active
Secret. The value is read when a call is made and written nowhere — not to a
log, not to any response. An env-var name still works (overlap with 3A). A
Secret is ciphertext under a wrapped DEK; rotating `TINY_HERMES_KEK` rewraps
DEKs and does not re-encrypt every payload.

Local Compose still passes `TINY_HERMES_MODEL_KEY_EXAMPLE` so an endpoint
registered against that name keeps resolving. To exercise rewrap, create a
platform Secret from `/workspaces/:id/secrets` (or `POST /api/v1/secrets` with
`scope: platform`) and register an endpoint whose `credential_ref` is that
Secret's id.

Set the variable *or* store the Secret first, then register the endpoint.
Registration refuses a `credential_ref` that is neither a defined environment
variable nor an active Secret id:

```powershell
$env:TINY_HERMES_MODEL_KEY_ACME = "your endpoint key"
$endpoint = Invoke-RestMethod -Uri "$api/model-endpoints" -Method Post -WebSession $browser `
  -ContentType "application/json" -Headers @{ "X-CSRF-Token" = $csrf } `
  -Body (@{
    name = "acme-gpt"; kind = "openai_compatible"
    base_url = "https://models.example.com/v1"; model = "acme-large"
    context_window = 128000; max_output_tokens = 4096
    context_accounting = "shared"; tokenizer = $null
    usage_quality = "provider"; credential_ref = "TINY_HERMES_MODEL_KEY_ACME"
  } | ConvertTo-Json)

Invoke-RestMethod -Uri "$api/model-endpoints/$($endpoint.id)/check" -Method Post `
  -WebSession $browser -Headers @{ "X-CSRF-Token" = $csrf }
```

The check makes one real request through the same guarded client a Run uses, and
answers with a verdict and a duration. It never reports the endpoint's status or
body: a `base_url` mistyped into an internal service would otherwise make that
route a way to read it.

### Secrets and KEK rewrap

`TINY_HERMES_KEK` is 32 bytes of standard base64. API `/health/ready` reports
`kek: current` or `kek: missing` and is 503 when missing. A Worker still starts
without it; unwrap at call time fails that call.

Create a platform Secret (console Secrets page, or `POST /api/v1/secrets` with
`scope: platform`). The response has a mask and never plaintext. `GET /api/v1/secrets`
lists names, scope, mask, status, and timestamps.

To rotate the KEK: set `TINY_HERMES_PREVIOUS_KEK` / `TINY_HERMES_PREVIOUS_KEK_ID`
to the pair that wrapped existing rows, set `TINY_HERMES_KEK` / `TINY_HERMES_KEK_ID`
to the new pair, restart the API, then `POST /api/v1/secrets/rewrap` as a platform
administrator. The response is `{ processed, remaining, current_key_id }`.
Interrupt and rerun; rows already on the new `key_id` are skipped. A database
backup without the matching KEK cannot decrypt.

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

### The context window, and what an Agent may plan to fill it with

`context_accounting` says whether the endpoint's window holds the output as well
as the input. `shared` is the default and the conservative answer: the reserved
output is subtracted, so a 128,000 window with 4,096 reserved has an input
allowance of 123,904. `separate` leaves the whole window for input. It is
declared rather than guessed from the provider's name, because two endpoints of
the same size hold different amounts of conversation depending on it.

`tokenizer` records a name and nothing more in this version. No tokenizer ships
verified here, so every endpoint is planned with a conservative
characters-based upper bound; the field exists so a verified implementation can
be added later without moving the planner. Every number the planner produces is
a plan estimate and is never added to `consumed_tokens` — billing still comes
only from what the provider reported.

Publishing checks that the endpoint can serve the Agent's segment budget. The
platform default sums to 9,472 tokens, so an endpoint whose *input allowance* is
below that is refused with `context_budget_unsatisfied`, and the refusal carries
a suggested number for every segment rather than the word "invalid". The advice
does not apply itself: take it into the draft and publish again. An endpoint
that cannot hold even the 768-token floor is `context_window_too_small`, which
no advice would fix.

At run time the same shortage is `paused(context_overflow)`: the round is not
sent, no model call is spent, and the transcript keeps every message. Before
that the platform trims the oldest large tool results (leaving each call
answered by a stub naming its `call_id` and its size) and then compacts the
oldest turns into one generated summary, recording the range and the ids it
stood in for. Both leave a `context_trimmed` or `context_compacted` event on the
Run, and the console says in words what each one did.

### External tools, and why a write stops

Two kinds, one shape. An **HTTP tool** is an OpenAPI document a workspace
registers; an **MCP server** is an address this platform reads a tool list
from. Both are registered under *HTTP 工具* and *MCP 服务* in the console, and
both refuse a host that is not already in the workspace's outbound scope —
approve it under *出站* first, or the registration is refused and names the
host.

An Agent binds a **version** and a **subset**, never the tool itself:

- HTTP: a document version plus the operation ids it may call.
- MCP: a server snapshot plus the tool names it may call. There is deliberately
  no way to bind "all of them" — a server that later advertises forty more
  would otherwise widen a published Agent with nobody publishing anything.

An MCP server's tool list is **re-read before every execution slice**, and only
the bound names are offered. If the bound schemas no longer fit the
`tool_schemas` segment the Run pauses with `tool_budget_exceeded` before it
spends a model call; nothing is truncated, because a schema cut down to fit
would leave a model calling a tool with arguments the far end never agreed to.
Resume it after binding fewer tools or raising the segment — the failed attempt
charged nothing, so it starts from the same place.

**Every binding that could change something needs a decision at publish**
(§16.3), and publishing fails without one. The three answers are:

- *直接拒绝* — the call is refused at runtime and nobody is ever asked.
- *已经预先批准* — a workspace administrator approved this narrow scope by
  publishing the version, so the call runs. Only a workspace administrator may
  publish one; a developer doing it would be granting themselves the approval.
- *每次都问管理员* — each call stops and waits.

For MCP the choice is required for *every* binding rather than only for ones
that write: a server does not say which of its tools change something, so the
platform cannot tell, and guessing "this one only reads" is the guess that
would be wrong quietly.

A Run that stops enters `waiting_approval`. It holds no lease and no container
while it waits, and it resumes only when somebody answers on the *审批* page —
never on its own. That page shows the **normalized call**, exactly as it was
hashed. Approving allows that call and no other: change any argument and the
approval stops covering it, and the Run asks again. Rejecting requires a
reason, because the person whose Run stopped is not the person who stopped it.
An unanswered approval expires (24 hours by default, configurable per
workspace between five minutes and seven days) and the Run pauses with
`approval_expired`.

Two rules the console cannot bend. Only the end user who started a Run may
answer a `user_confirmation`; an administrator who thinks the action should
happen opens a governance approval and writes down why, and never answers in
the user's name. And a decided approval is never decided again — an override is
a new record, not a rewrite of somebody else's.

### What a Run cost, and what the platform will not claim

Prices live on the model endpoint, as versions rather than as a current value.
Only a platform administrator sets one:

    POST /api/v1/model-endpoints/{id}/pricing
    {"currency": "USD", "input_per_million": "3", "output_per_million": "15"}

Amounts are **strings**, and they are parsed with `Decimal`. A JSON number
would be a float before the server could object, and what got stored would not
be what was typed.

Entering a price never edits the old one. A Run fixes the version that was in
force when it was created, so a correction entered tomorrow does not rewrite
what today's Runs cost — which is the whole reason the old row has to survive.

**No price is not a price of zero.** An endpoint an administrator priced at
zero has a row saying so and reports a cost of `0`; an endpoint nobody has
priced reports `unknown`, and the console says so in those words rather than
showing a zero. The difference matters most when a workspace has a spending
limit: a Run whose cost cannot be counted is **stopped** rather than allowed
through on a total nobody is keeping. Set the limit on the workspace
(`max_run_cost` and `cost_currency`); leave it null for no limit, in which case
an unpriced endpoint costs the Run nothing.

Every model call is checked before it is made, against the estimated input and
the *largest* output the endpoint may produce. There is one honest gap, and it
is worth knowing rather than discovering from a bill: a streaming provider only
reports its final usage when the round ends, so **a single call may pass the
limit before the platform can see that it did**. The limit stops the next one.

An endpoint registered with `usage_quality=unavailable` reports no token counts
at all. Its token and money ceilings are therefore disabled — there is nothing
to count them against — while the elapsed-time limit, the model-call limit and
the per-call maximum output stay enforced. Such an endpoint is not unlimited;
it is limited by the three valves that do not depend on it reporting anything.

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
- Environment variables can be visible through process and container inspection. Local Compose sets `TINY_HERMES_KEK` for API ready; a production deployment must mount the KEK from a protected file or KMS.
- M1 through 4C provides Agent publication, Run execution, model endpoints,
  platform-owned `file.*` and `shell.exec` in a Docker sandbox, persistent
  `/workspace/data`, tenant-scoped Artifacts, ServiceAccount API Keys,
  inbound Chat Completions, the Playground console, and Secret envelopes.
  Approval queues remain later slices; no tool may execute on the API, Worker,
  or host directly.
- Agent personality text is never echoed in an error response, and a resource in another workspace always answers with a generic `404`.
- A wake-up message carries a workspace ID and a Run ID and nothing else. Redis never sees a Run's input, an Agent's personality, or any other content.
- The event stream selects its workspace through a query parameter because `EventSource` cannot send a header. Authorization is still the session cookie, and a Run in another workspace answers `404` whatever the parameter says.
- Model endpoint credentials are an environment variable name **or** a Secret
  id. The platform never returns plaintext after create. API `/health/ready`
  is 503 without a valid `TINY_HERMES_KEK`; a Worker still boots and fails the
  call if unwrap is needed.
- Everything that leaves the process goes through `tiny_hermes.outbound`, and `ruff` fails the build on a raw HTTP client or socket built anywhere else. The check is not advisory.
- A refused outbound address is reported to a workspace member as a code only. The resolved address goes to the audit trail, because a refusal that names an internal IP is a way to map the network the platform runs on.
- The endpoint connectivity check reports a verdict and a duration, never the endpoint's status or body. A `base_url` mistyped into an internal service would otherwise turn that route into a way to read it.
