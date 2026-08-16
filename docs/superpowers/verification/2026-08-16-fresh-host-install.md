# A fresh host, installed from the documentation — 2026-08-16

## 1. Scope

Product roadmap §7 gate 2: *a fresh Linux Docker host can be started by
following the documentation and complete a single-agent file and command
task*, and gate 3: *the release carries no real Secret, local data, test
artifact, or undeclared dependency*.

The rule for this walk was that only `docs/development.md` counts. Nothing
learned from the previous two hosts was used to skip a step, and every place
the document did not answer a question is recorded in §4 as a defect in the
document rather than worked around silently.

Host address, passwords, the bootstrap token, and the API key are absent from
this file.

## 2. The host

| Fact | Measured |
|---|---|
| OS | Ubuntu 26.04 LTS, Linux 7.0.0-14-generic |
| vCPU | 8 |
| MemTotal | `15850220 kB` ≈ 15.12 GiB (a 16 GB machine) |
| Disk | `vda` 50 GiB, `ROTA=1` (virtio) |
| Pre-installed | `git`, `curl`, `python3` (3.14.4) |
| Absent | `docker`, `uv`, `node`, `pnpm`, and any `python` binary |

Tree: `main` at `0a201b5`, delivered as a git bundle. The host holds no
repository credential.

## 3. What the documentation produced

Every step below is the document's own, in its order.

| Step | Result |
|---|---|
| `uv sync --frozen` | ok, Python 3.12 fetched by uv |
| `corepack pnpm install --frozen-lockfile` | ok, 2.6s, pnpm 10.15.0 |
| `scripts/generate_local_secrets.py --env-file .env` | ok; the generated file documents the sandbox digest in its own comments |
| `docker compose … up -d --build --wait` | **exit 0**, all nine services healthy on the first attempt |
| sandbox image build + `SANDBOX_IMAGE_DIGEST` | ok, digest `sha256:08c5478c…` approved |
| `POST /api/v1/bootstrap` | **201**; a second call is `bootstrap_closed` |
| `POST /api/v1/auth/sessions` | 201, CSRF cookie present |
| two workspaces | 201, 201 |
| Agent `Analyst` / `analyst`, draft, publish | version 1 published |
| ServiceAccount + API Key | token returned once; the listing contains no `token` |
| `POST /v1/chat/completions` | **200**, content `exit=0` + the command's output |
| leftovers | 0 containers labelled `tiny-hermes.run-id` |

### 3.1 The file and command task

One Session, `shell_from_input`, tools `shell.exec` / `file.read` /
`file.write` / `file.list`. Two Runs, both `completed`. The transcript is the
evidence:

```
1 user      sh -c 'echo "written on a fresh host" > /workspace/data/report.txt && ls -l /workspace/data'
2 assistant tool_call shell.exec
3 tool      output: total 4  -rw-r--r-- 1 sandbox sandbox 24 … report.txt   exit_code 0
4 assistant exit=0 …
5 user      cat /workspace/data/report.txt
6 assistant tool_call shell.exec
7 tool      output: "written on a fresh host"   exit_code 0
8 assistant exit=0 …
```

The second Run ran in a **fresh container** — its own `sandbox_cache_reset`
precedes it — and read back what the first one wrote. A file crossing
containers inside one Session is the whole point of the gate.

Note for anyone repeating this: a tool round leaves no `run_events` row. The
first reading of this walk called the command "never executed" because
`run_events` shows only `run_created`, `run_lease_acquired`,
`sandbox_cache_reset`, `run_completed`. The transcript in `session_messages`
is where a tool call and its result live.

### 3.2 Release hygiene (gate 3)

- No `.env`, key, certificate, or credential file is tracked. `.gitignore`
  covers `.env`, `.env.*`, `data/`, `.uv-cache/`, `node_modules/`.
- `.env.example` carries placeholders and local-only Compose defaults, no
  real values.
- No test artifact, dump, database file, or log is tracked.
- A scan for AWS keys, `sk-…` tokens, and PEM private-key headers across
  every tracked file returns nothing.
- `pyproject.toml` description is `单 Agent 安全运行骨架`.
- On the running stack: the Controller has the Docker socket and neither an
  object-store credential nor the KEK; the API and the Worker cannot see the
  socket at all.
- The host's working tree is clean; the generated `.env` is ignored.

## 4. Where the documentation failed

Every one of these cost real time on a host whose only instructions were this
document. They are ordered by how early a reader meets them.

1. **No install instructions at all.** `## Requirements` lists five tools with
   exact versions and a block of `--version` checks, and never says how to
   obtain any of them — on a document whose own walkthrough begins "on a fresh
   Linux Docker host".
2. **Every command block is PowerShell.** Not one of them runs on the Linux
   host the walkthrough addresses. The API calls had to be re-derived as
   `curl`.
3. **`python --version` cannot work on Ubuntu.** There is no `python` binary;
   `python3` is **3.14.4**, while the document requires 3.12. `uv` fetches
   3.12 itself, so the requirement is satisfied invisibly — but the
   document's own check misleads a reader into thinking the host is wrong.
4. **Node 24 is not installable from the distribution.** Ubuntu 26.04 offers
   `22.22`; the document requires 24 LTS and names no source. The tarball
   from nodejs.org was used.
5. **How to obtain the source is never stated.** No clone URL, no note about
   credentials for a private repository. This walk used a git bundle.
6. **The sandbox image is missing from *First start*.** A tool-bound Agent
   cannot run without `SANDBOX_IMAGE_DIGEST`, and the build command lives 500
   lines below, under a phase heading. The generated `.env` does explain it in
   comments, which is the only reason the ordering is survivable.
7. **The walkthrough assumes a browser on the host.** "Open
   `http://127.0.0.1:3000`" has no meaning over SSH; port forwarding is not
   mentioned.
8. **An unattended `apt-get` can hang forever.** Ubuntu's `needrestart` opens
   a dialog after a libc update, and with no terminal the install is stopped
   by `SIGTTOU` rather than failing. On the previous host that cost 47
   minutes. Any install section this document grows needs
   `DEBIAN_FRONTEND=noninteractive` and `NEEDRESTART_MODE=a`.

None of these is a product defect. All of them are reasons an operator
following this document alone would stall, which is exactly what gate 2 is
meant to detect.

## 5. Verdict

**Gate 2 passes on the product and fails on the documentation.** A fresh
Linux host reached a working single-agent file-and-command task, Chat
Completions included, with no product change and no workaround — but only
because the operator could supply the five missing install steps. A reader
with the document alone stalls at §4.1.

**Gate 3 passes.**

## 6. Not claimed

- `0.1 Technical Preview`.
- That the documentation is fixed. §4 is a list of defects, not a changelog.
- The console UI itself. The walkthrough's browser steps were exercised as
  their documented API equivalents; Playwright covers the console in CI.
- Local SSD. This host's disk is virtio, `ROTA=1`.
- A §24.1 benchmark run on this host. It was not the question here.
