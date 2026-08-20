# M2C external tools, approvals and the cost valve — phase exit verification, 2026-08-18

## 1. Scope

This record covers M2C in both halves: M2C-1, the outbound boundary, planned in
`docs/superpowers/plans/2026-08-18-m2c-egress-proxy.md`; and M2C-2, the tools
and approvals that run on it, planned in
`docs/superpowers/plans/2026-08-18-m2c-tools-approvals.md`. Product design
§12.4, §16.1, §16.2, §16.3, §16.5 and §20; M2 roadmap §6.

One sentence for the stage: an Agent can now reach outside the platform, and
every way it does is something a person chose, measured at a boundary that is
a separate process, and stopped before it writes.

Commits on `feat/skills`, continuing after M2B's record at `41a116c`:

| Commit | Result |
|---|---|
| `d61588a` | The M2C-1 plan. |
| `19321a1` | Four layers of "may this go out", and one operation on them. |
| `6034c87` | A process whose only job is to say no to a packet. |
| `861ca8b` | The client cannot reach past the boundary any more. |
| `353ce8c` | Two levels of approval, and an Agent that chooses inside both. |
| `6c00a7a` | The sandbox reaches the network through the proxy or not at all. |
| `e4004bb` | The M2C-2 plan. |
| `52f7c07` | Decide what somebody else's API description may become. |
| `9b8d0a3` | A read may go out, and a write waits for a person who is not there yet. |
| `4718bb9` | The later migration numbers move up with the approvals table. |
| `bd2e55e` | A write stops for a person, and only that person may answer. |
| `a127151` | A subset somebody chose, measured before the Run spends anything. |
| `aafa8a1` | Unknown is not zero, and a limit that cannot count refuses. |
| `052a1c9` | The two approval kinds apart, the tools that need them, and what a Run cost. |
| this record | The six exit criteria against evidence, and what the run did not prove. |

## 2. Environment

Windows 11, Python 3.12 via uv, Node via pnpm. Backend integration tests run
against a local PostgreSQL 17 container on port 54320, migrated to
`20260818_0023`.

The browser walk runs against `deploy/compose/compose.yaml` brought up as an
isolated project (`-p tiny-hermes-e2e`) with its own volumes and its own host
ports (3100 and 8100), built from this branch. The pre-existing local demo
stack was left running and untouched throughout. The port override and the
environment file live in a scratch directory rather than in the repository,
because they are facts about this host.

That stack is the first one on this machine to run the **egress proxy**:
`EGRESS_PROXY_URL`, `EGRESS_PROXY_TOKEN` and `OUTBOUND_ALLOWED_CIDRS` are set,
and the proxy logs `approved_networks=1 ports=[80, 443, 8000]` at start.

`SANDBOX_IMAGE_DIGEST` was **not** set for this run. Two earlier walks
(`console`, `skills`) bind `shell.exec` and cannot complete without it; see §5.

## 3. What ran

| Suite | Result |
|---|---|
| `ruff check packages/backend migrations` | clean |
| `pyright packages/backend` | 0 errors |
| `pytest packages/backend/tests/unit` | 1598 passed |
| `pytest packages/backend/tests` (unit + integration) | 2144 passed, 17 skipped, **1 failed** — see §5 |
| `alembic upgrade head` / `check` / `downgrade -1` / `upgrade head` | clean for 0016–0023 |
| `tsc --noEmit` (console) | clean |
| `eslint .` (console) | clean |
| `vitest run` (console) | 145 passed |
| `playwright --project=tools` | passed, three separate runs |
| `playwright` (all projects) | 7 passed, 3 failed — see §5 |

## 4. The six exit criteria

The M2 roadmap §6 lists six. Each is answered by evidence rather than by
assertion, and where the evidence is narrower than the claim that is said here
rather than left for a reader to notice.

**1. With `egress-proxy` stopped, every outbound tool and model call fails, and
no path falls back to a direct connection.**

Proven twice and in two different ways. `SafeOutboundClient` built without an
`EgressRoute` has `self._client is None` and raises `OutboundRefused(
egress_not_configured)` on every call — there is deliberately no branch that
connects directly, and `tests/unit/outbound/test_proxy_ban.py` walks the AST of
`client.py` to assert every httpx client is constructed with `proxy=` and
`trust_env=False`, and that nothing outside `egress/` and `sandbox/transport/`
calls `open_connection`, `create_connection` or `open_unix_connection`. The
second half is the one that matters: it fails on a *new* code path that reaches
past the boundary, not only on a regression in this one.

**2. A target the workspace allows and the Agent does not is refused; a Run
layer may narrow and never widen.**

`intersect()` of no layers is **empty, not everything**, and a layer that is
named but unknown is empty rather than absent — both pinned in
`tests/unit/outbound/test_scope.py`. At publish, `AgentCatalog._check_network`
refuses every entry outside the workspace's and carries all of them back.
`tests/integration/runs/test_http_tool_calls.py` proves the runtime half
against a real proxy reading the real SQL directory: the workspace entry is
revoked *after* the tool was registered and the Agent published, and the next
call is refused at the boundary — which is only possible if the claim reaches
the proxy and is looked up rather than believed.

**3. A Run whose MCP schemas exceed the budget enters
`paused(tool_budget_exceeded)`, and a resumed Run is not charged twice.**

`tests/integration/runs/test_mcp_tools.py` does exactly this against a real
stand-in MCP server: the server's schema grows past the `tool_schemas` ceiling,
the Run pauses with `tool_budget_exceeded`, a `tool_schema_budget_exceeded`
event carries both numbers, the stand-in receives no call, and
`consumed_model_calls` is **0**. The server then shrinks, a person resumes, and
the Run completes with `consumed_model_calls == 2` — two rounds, both after the
resume. Nothing is truncated at any point.

**4. An end-user identity cannot approve a governance operation.**

`may_decide` in `runs/domain/approval.py` answers this in both directions, and
`tests/unit/runs/test_approval.py` pins both: an end user may never answer a
governance approval, and — the one that matters more — an administrator may
never answer somebody's `user_confirmation`, even holding both admin flags.
The service refuses a service account outright.

**Narrower than the claim:** this has unit coverage only. See §5.

**5. Changing a tool's arguments after approval invalidates it and the Run
queues again.**

`normalize_call` hashes the tool, every argument, the working directory, the
target and the permission; `is_still_valid` compares that hash. Eleven unit
tests cover one thing each that must invalidate and the single thing that must
not (argument order). `tests/integration/runs/test_approvals.py` then asks the
real gate: after a person approves, the same normalized call answers
`approved` and a call with one extra argument answers `requested` — a fresh
question, from the same Run.

**6. On a `usage_quality=unavailable` endpoint the currency ceiling is disabled
while time, call count and maximum output stay enforced; unknown cost is never
recorded as zero.**

`enforced_valves` returns all five flags together and one test asserts all five
at once, because checking only the disabled half is how a reader comes away
believing such an endpoint is unlimited. The "unknown is not zero" half runs
through the whole of §4 as paired tests: the same call priced at zero and
unpriced must not produce the same answer, and
`tests/integration/runs/test_cost_valve.py` proves the consequence — a Run with
a spending limit and an unpriced endpoint is **stopped**, with
`consumed_model_calls == 0` and an event whose reason names the missing price.

## 5. What this run does not prove

**The Windows sandbox test failed, and it is not this branch's.**
`test_engine_workspace.py::test_execute_feeds_stdin_and_the_helper_writes_
atomically` raises `TypeError: 'NpipeSocket' object does not support the
context manager protocol` at `docker_engine.py:312`. That line landed on
2026-08-13 and this branch does not touch the file; the suite has simply not
been run on Windows with Docker since. A separate task is open for it.

**Two browser walks could not run here.** `console` and `skills` bind
`shell.exec`, and this acceptance stack has no `SANDBOX_IMAGE_DIGEST`. They are
not evidence for or against this stage; the M2B record covers them on a stack
that had one.

**The tools walk failed once inside the full sequential run.** On its own it
passed three times, including once with all the earlier walks' data already in
the platform. In the full run it failed at the first Run submission — the page
stayed on the runs list. I did not establish why, and I am not going to claim
it is only the two sandbox-less walks ahead of it, because I did not prove
that.

**Nobody has seen the console against a live API except through this walk.**
The three new pages have component tests that render them against mocked
responses, and the Approvals page and the HTTP tools page are exercised by the
browser walk. The MCP page is not: no walk registers a server, so its
rendering has only component coverage.

**The 403 for an administrator reaching at a user confirmation is unit-only.**
The integration suite has a single bootstrapped platform administrator, and a
second signed-in member needs a user-creation path this phase does not have.
`may_decide` is exhaustive in both directions; nothing has driven it through
the HTTP face.

**No `user_confirmation` has ever been produced by a running Run.** Every
approval this stage creates is a `governance_approval`: an external write is on
§16.3's governance list, and MCP calls are treated as governance because the
platform cannot tell which of them write. The user-confirmation path is built,
tested at the unit level and reachable through the API — but no tool in M2C
asks for one.

## 6. What this stage does not claim

**A pre-authorization is not an automatic approval.** Choosing "approved in
advance" records that a workspace administrator looked at this narrow scope and
agreed to it, by publishing the version. It does not check anything at runtime,
and it does not ask anybody. An Agent published with it will write without
stopping — which is the point, and the reason only a workspace administrator
may publish one.

**The schema budget is an estimate, not a measurement.** `estimated_tokens_of`
counts characters and divides. It decides what to send and never what to bill,
and a Run that fits by a hair on this estimate may not fit on the endpoint's
own count. §9.4's rule stands: a real estimate needs a tokenizer verified
against the model, and none ships.

**Unknown cost is not zero, and this platform will not guess.** An unpriced
endpoint makes a Run's cost unknown for the rest of its life, and one unpriced
round makes the whole total unknown rather than understating it. Two currencies
in one Run do not add. A streaming provider reports usage only at the end, so a
single call may pass a spending limit before the platform can see that it did —
the limit stops the next one, and that is written in `docs/development.md` and
in this record rather than left to a bill.

**An MCP server's replies are untrusted text.** A tool description and a
`tools/call` result are somebody else's words on their way into a model's
context, exactly like skill text. Nothing in this platform resolves, expands or
follows anything a server says. The bound name subset is the control; the
server's own discovery is not a permission.

**The tool catalogs hold references, never credentials.** `credential_ref`
names an environment variable or a Secret, resolved at the moment of the call
on the platform's side of the boundary, put in a header, and returned nowhere.
No tool result, event, log line or approval document has ever held one.

## 7. One thing this acceptance run found

The walk's first real call was refused by the boundary with `port_not_allowed`,
because the shipped policy allows 80 and 443 and the target listened on 8000.
That is the proxy working. But the comment on `ALLOWED_PORTS` had always said a
platform administrator may widen it, and nothing let them: there was no
setting. An installation could therefore bind only tools that happen to live on
the public web's two ports, which is not a decision anybody made.

`EGRESS_ALLOWED_PORTS` now exists — empty means the shipped pair, a bad value
stops the process where an operator is looking, and widening is a platform
administrator's decision and only theirs. `.env.example` and the Compose file
carry it. This is the kind of thing an acceptance step exists to find: every
unit test in the stage passed without it.

Two smaller ones, both found by the same walk and both real defects rather than
test friction: the outbound page's two forms generated colliding control ids,
so the second form's label pointed at the first form's input; and the Approvals
page rendered only a call's `arguments`, which meant a call with none — most of
them — was reviewed as an empty object with no target in sight.
