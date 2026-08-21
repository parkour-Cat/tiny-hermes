# End-user entry — phase exit verification, 2026-08-21

## 1. Scope

This record covers all of the end-user entry work, planned in
`docs/superpowers/plans/2026-08-20-end-user-entry.md` against design
`docs/superpowers/specs/2026-08-20-end-user-entry-design.md` and product
design v2.5 §4.5.1–§4.5.4, §4.6, §278, §282, §344, §348, §928.

One sentence for the stage: somebody the platform has never met — a
customer of an enterprise that has integrated tiny-hermes — can open a chat
surface embedded in that enterprise's own page, be recognized across visits
as the one subject their employer's signature says they are, keep memories
and Sessions that are theirs and nobody else's, and be read by a developer
troubleshooting them only ever with a trail left behind; the platform itself
never learns their name, never issues them a password, and never merges what
it knows about them across channels.

Commits on `feat/end-user-entry`, from `530ba97` (branch point, M2 complete)
to `3cc8470` (this task's own head), 61 commits over seven implementation
tasks plus this one:

| Task | Commits | What it built |
|---|---|---|
| §1 identity skeleton | `a4bda2b`..`0f0665a` (+ plan fix `47cab55`) | Migration `20260820_0030`: `end_users`, `external_identities`, `channel_issuers`. `CallerType` gains `end_user`. `MemoryScope` re-proven to still reject "every subject" with a third caller kind in play. |
| §2 signature verification | `9aa7f2d`..`ebd0021` (incl. `190bc89`) | `identity/domain/end_user_credential.py::verify()` — pure, no I/O. `RS256`/`ES256` only, 15-minute platform ceiling enforced from `now` not the token's own `iat`, every failure but one collapsed into a single 401. |
| §3 credential exchange | `ebd0021`..`916acf7` (+ fixes `e4ff014`..`46a8284`) | `POST /api/v1/end-user/sessions`, `channel_issuers` CRUD, `DELETE /end-user/sessions/{id}` revocation, `resolve_end_user_caller` kept structurally apart from the platform-member path, every console endpoint proven 403 to an end-user cookie. |
| §4 two-gate Agent access | `88be3aa`..`23ca589` | `AgentSpec.end_user_access` (ninth optional document, absent-means-unchanged-hash re-pinned), `AgentCatalog.resolve_end_user_agent` intersecting the platform gate with the credential's own `agents` claim. |
| §5 Run and memory wiring | `23ca589`..`d5fb5e8` (+ fixes `5a84ff8`..`a23f748`) | Migrations `20260820_0031`–`20260820_0033`: session store, `memories.subject_type` widened, `runs.end_user_id` and `approvals.requested_by` off their `users`-only FKs. `USER_CONFIRMATION` gets a producer *and*, once review caught the gap, a consumer — the end user's own approval door. |
| §6 read-leaves-a-trace | `b2fcc31`..`544d699` | A developer's read of an end user's session messages writes `end_user_session.read`; listing does not; correct/delete/forget stay closed to a developer. |
| §7 chat surface + origin enforcement | `544d699`..`fa3a4f4` (+ fixes `a8cbae8`..`3cc8470`) | `apps/chat-web` swapped onto the end-user session cookie; migration `20260820_0034` records which issuer minted a session; `Origin`/`Referer` checked against that one issuer's `allowed_origins`, never the workspace union; the credential moved from the URL query string to the fragment. |
| §8 this record | this commit | Everything below. |

## 2. Environment

macOS 14 (Darwin 23.6), Python 3.12 via uv, Node 24 via pnpm 10, Docker 29 via
OrbStack.

Backend integration tests and the migration round trips ran against a
PostgreSQL 17 container on `127.0.0.1:55432` (`th-test-pg`), separate from the
Compose stack's own `postgres` service, for the same reason M2E's record gives:
this host's loopback already shadows 5432, and a dedicated port removes the
variable entirely.

The browser walk ran against `deploy/compose/compose.yaml`, rebuilt from this
branch's HEAD (`3cc8470`). Bringing it up needed five environment variables the
compose file defaults to empty or absent, none of which are secrets:

- `SANDBOX_IMAGE_DIGEST`, from a local build of `deploy/sandbox/Dockerfile`
  (`docker image inspect tiny-hermes-sandbox:local --format '{{.Id}}'`).
- `OUTBOUND_ALLOWED_CIDRS=192.168.107.0/24,192.168.117.0/24` — OrbStack's two
  bridge subnets for this stack's `default` and `sandbox-egress` networks (M2E's
  record found the same shape of thing on the same class of host: CI's Docker
  gives `172.16.0.0/12`, OrbStack does not).
- `EGRESS_PROXY_URL=http://egress-proxy:3128` and `EGRESS_PROXY_TOKEN`, without
  which the `egress-proxy` container refuses to start at all
  (`EGRESS_PROXY_TOKEN is required to run the egress proxy`) — it is not
  optional infrastructure, and this stack had never been brought up with it set
  on this host before.
- `EGRESS_ALLOWED_PORTS=80,443,8000` — without it every outbound call the
  `tools` walk makes is refused `port_not_allowed` even with the CIDR open; the
  bridge being reachable and the port being approved are two separate checks
  and both failed independently on the first attempt before this was found.

Two things worth recording exactly because they cost real time and will cost
the next person the same time if this is not written down:

**Recreating `api` without restarting `web`/`chat-web` gives every proxied
route a 502.** `nginx` resolves `api` once at container start; `docker compose
up --wait` recreated `api`, `worker`, `controller` and `scheduler` (their
config changed with the new env vars) but left `web` and `chat-web` alone
(theirs had not), and both then 502'd on every `/api/` and `/health/` path
until explicitly restarted. This is the identical finding M2E's record made
about the same nginx behavior; it recurred here because the fix is a step in
a runbook, not a property of the stack.

**`EGRESS_ALLOWED_PORTS` is a second, independent gate from
`OUTBOUND_ALLOWED_CIDRS`.** The first attempt at the `tools` walk set the CIDR
correctly and still failed — `egress-proxy`'s own log said `egress refused:
caller=platform host=api reason=port_not_allowed`, not a CIDR refusal, because
the tool in that walk calls the platform's own API on `:8000` and the port
allowlist defaults to empty independently of the CIDR allowlist. CI's own
config (`EGRESS_ALLOWED_PORTS: "80,443,8000"`) already carries this; nothing
in `docs/development.md`'s existing sandbox/egress walkthrough mentions it, and
this record is the second time (after the CIDR itself) that a value CI sets
for granted has had to be rediscovered by hand on this host.

## 3. What ran

| Suite | Result |
|---|---|
| `ruff check packages/backend migrations` | clean |
| `pyright` (backend, tests, scripts) | 0 errors, 0 warnings |
| `pytest packages/backend/tests/unit` | 1868 passed in 6.5s |
| `pytest packages/backend/tests/integration` (less `sandbox/`) | 593 passed in 445.5s |
| `alembic upgrade head` | clean, lands on `20260820_0034` |
| `alembic check` | clean, after the one-line `env.py` fix §5 describes |
| `alembic downgrade` each of 0033→0029, then `downgrade base`, then `upgrade head` | clean at every step |
| console (`apps/web`): `vitest run` | 152 passed, 22 files |
| console: `eslint . --max-warnings 0` | clean |
| console: `tsc -b && vite build` | clean |
| `chat-web` (`apps/chat-web`): `vitest run` | 38 passed, 13 files |
| `chat-web`: `eslint . --max-warnings 0` | clean |
| `chat-web`: `tsc --noEmit && vite build` | clean |
| `playwright` (all 8 projects, Compose stack built from `3cc8470`) | **14 passed, 0 failed** |

`packages/backend/tests/integration/sandbox/` is excluded from the integration
number above and is covered on its own in §5: it is a pre-existing,
environment-specific failure that predates this branch.

## 4. The exit criteria, section by section

Each heading below is the plan's own 出口检查 (exit check) for that section,
paraphrased, against the evidence for it.

**§1 — `alembic upgrade head` then `alembic check` clean; downgrade to 0029 and
back clean; the third `CallerType` value does not break an existing Run's
read/write.**

The round trip is clean, and so is `alembic check` — but only after a wiring
bug this record's §5 describes was fixed. It was never this section's own
migration; it was `migrations/env.py` failing to import the module that carries
0031's table. `test_caller_type_check_constraints_admit_end_user`
(`tests/integration/identity/test_end_user_identity_migration.py`) covers the
CHECK-constraint half directly; the unit suite's pre-existing Run tests all
passing unmodified (§3's 1868) is the evidence that a third enum value did not
disturb the first two.

**§2 — an HMAC-resigned token is refused; an `alg: none` token is refused; a
24-hour `exp` is refused with a distinguishable reason.**

All three are named tests in `tests/unit/identity/test_end_user_credential.py`:
`test_a_token_resigned_with_hmac_using_the_public_key_as_secret_is_refused`,
`test_an_alg_none_token_is_refused`, and
`test_exp_past_the_15_minute_ceiling_is_refused_with_a_distinguishable_reason`
(with the boundary itself pinned by
`test_exp_exactly_at_the_15_minute_ceiling_still_verifies`). The file carries
one test per named failure mode in the plan — wrong signature, wrong `iss`,
wrong `aud`, expired, not-yet-valid, disabled issuer, `alg: none`, alg
confusion — 19 tests in total, each isolated so a fix to one does not need to
touch another.

**§3 — one full exchange; a repeat exchange is idempotent to the same subject;
a disabled issuer's new credentials are refused immediately while its already-
exchanged sessions are not; revocation invalidates a session immediately; every
end-user cookie is 403 on every console endpoint.**

`tests/integration/identity/test_end_user_sessions.py` carries all five, by
name:
`test_a_registered_issuers_credential_exchanges_for_a_session`,
`test_the_same_sub_exchanging_twice_gets_the_same_end_user_id`,
`test_disabling_an_issuer_refuses_new_credentials_but_not_the_already_exchanged_session`
(asserting the **known, accepted** asymmetry the plan calls for, not its
absence),
`test_revoking_an_end_users_sessions_invalidates_the_cookie_immediately`, and
two tests — `test_an_end_user_session_cookie_cannot_reach_console_endpoints`
and `test_an_end_user_session_cookie_cannot_reach_this_tasks_own_admin_routes`
— covering the plan's explicit worry that a *new* admin route this task itself
adds could be the one console endpoint that slips through. A raced first-time
exchange (two tabs, same subject, both first) is its own test,
`test_two_simultaneous_first_time_exchanges_for_the_same_subject_both_succeed`,
and the review that found this gap is recorded in `progress.md` against task 3.

**§4 — four combinations of platform gate × credential's own `agents` list, one
test each.**

`tests/unit/agents/test_end_user_access.py` has exactly that structure —
`test_gate_open_and_listed_is_permitted`,
`test_gate_open_but_not_listed_is_refused_as_not_assigned`,
`test_gate_closed_but_listed_is_refused_as_gate_closed_and_names_the_alias`,
`test_gate_closed_and_not_listed_is_refused_as_not_assigned` — plus two more
the plan's four-combination table does not itself name but the two-gate design
requires: an Agent that never declared `end_user_access` at all refuses the
same as gate-closed
(`test_an_agent_that_never_declared_end_user_access_is_refused_as_gate_closed`),
and an alias from another workspace is never resolved
(`test_an_alias_from_another_workspace_is_never_resolved`). The ninth-document
hash promise is `test_an_agent_that_never_declared_end_user_access_hashes_as_it_always_did`,
run against `AgentSpec`'s `DETERMINISTIC_HASH` fixture the same way each of the
eight prior optional documents was pinned.

The two refusals stay distinguishable all the way to the wire — the plan's own
"the fix is in different people's hands" requirement —
`tests/integration/runs/test_end_user_agent_gates.py`'s
`test_listed_but_gate_closed_names_the_alias_as_the_admins_problem` versus
`test_gate_open_but_unlisted_says_nothing_the_end_user_could_learn_from`, the
second one a direct answer to the Minor task 4's review left open (an early
draft's refusal text carried the credential's own alias into an end-user-
facing message; the wired version does not).

**§5 — 小张's second visit is remembered; 小张 and 小王 cannot see each other's
memory; 小张 can export his own data; erasure removes it from retrieval.**

`tests/integration/runs/test_end_user_memory.py` carries the caller-type
assertion (`test_an_end_users_session_is_recorded_as_caller_type_end_user`),
the "the Run confirms it is really talking to this person"
(`test_the_run_confirms_to_the_real_end_user`), and the M2D isolation test
re-run against an `EndUser` subject —
`test_a_subject_is_told_their_own_memory` and
`test_another_end_users_memory_is_not_in_the_request`, both asserting the
bytes sent to the model, the same discipline M2D and M2E used, not a query
result. `tests/integration/runs/test_end_user_subject_self_service.py` covers
export (`test_an_end_user_can_export_their_own_memory`) and erasure
(`test_erasure_removes_what_export_could_see`).

The plan-gap review found mid-task — `USER_CONFIRMATION` had a producer with
nowhere for its own subject to answer it, because the approval route was
`_CONSOLE_ONLY` — is closed by
`tests/integration/runs/test_end_user_approvals.py`:
`test_the_write_opened_a_user_confirmation_not_a_governance_approval` (the
producer), `test_the_end_user_approves_their_own_confirmation_and_the_write_happens`
and `test_the_end_user_rejects_their_own_confirmation_with_a_reason` (the
consumer), and two refusals that keep the door narrow —
`test_a_different_end_users_confirmation_stays_out_of_reach` and
`test_a_governance_approval_is_refused_to_an_end_user_regardless`, the second
one the "no exceptions" half: an end user can never answer a governance
approval, ever, even one attached to their own Run.

**§6 — reading a session's content writes an audit row naming both parties;
listing does not; correct/delete/forget stay closed to a developer.**

`tests/integration/runs/test_end_user_session_audit.py`:
`test_developer_reads_end_user_session_content_and_it_is_audited`,
`test_listing_sessions_writes_no_audit_row`, and
`test_a_developer_cannot_correct_forget_or_erase_an_end_users_memory`.

**§7 — e2e green; the console's own Playwright walk unaffected.**

`tests/e2e/end-user.spec.ts` — one test, an enterprise signs an RS256
credential with a freshly generated keypair (not PyJWT — `node:crypto`
directly, deliberately independent of the library the backend trusts),
registers the issuer and publishes an `end_user_access`-enabled Agent through
the console's own signed-in session, then opens the chat surface in a *second*,
cookie-free browser context, has a conversation, closes and reopens the tab and
finds the same conversation, and exports the subject's own data through the
self-service door. All 8 Playwright projects — `foundation`, `console`,
`skills`, `tools`, `children`, `memory`, and `end-user`, 14 tests — passed
against the rebuilt stack (§3). The console's own six projects being
unaffected is direct evidence for "the console's own walk unaffected," not an
inference from the backend suite.

The origin-enforcement half of §7 has its own suite,
`tests/integration/runs/test_end_user_origin_enforcement.py`:
`test_a_write_from_an_unregistered_origin_is_refused`,
`test_a_write_from_the_registered_origin_still_succeeds`, and
`test_a_write_from_a_different_issuers_origin_in_the_same_workspace_is_refused`
— the last one is the task-7 review finding (§7 commit table) that the origin
check used to union every active issuer's origins rather than scoping to the
one that minted the session being written to, fixed and re-pinned. The fourth
test in that file,
`test_a_write_with_no_origin_or_referer_still_succeeds`, is not a gap found
and closed — it is pinning the deliberate design choice §5 below discusses.

The credential-in-fragment-not-query-string half is `apps/chat-web`'s own
`App.test.tsx` (`a credential in the URL exchanges a session and opens the
chat`, `a refused credential explains itself instead of offering a form to
retry in`) plus `tests/e2e/end-user.spec.ts` itself, which builds its URL with
`#credential=` rather than `?credential=`.

## 5. What this run did not prove

**`alembic check` failed on a migration-environment wiring gap, now fixed.**
`migrations/env.py` imports every table module by name so `Base.metadata` is
complete when Alembic compares it against the live database.
`end_user_tables` (migration 0030's three tables) was in that list;
`end_user_session_tables` — 0031's `end_user_sessions`, extended by 0032's
`agents` column and 0034's `channel_issuer_id` — was not. The table existed in
the database, created by `op.create_table` directly rather than by
autogenerate, and every migration and integration test touching it passed,
because none of them go through `Base.metadata`. Only `alembic check` does, and
it reported `end_user_sessions` as a table to **remove**.

Worth keeping in this record rather than quietly fixing, for two reasons. It
reproduced identically twice, so it was never a flake. And **CI runs the same
`alembic check`**, so it would have failed the branch — a test suite that is
593-green while the one command comparing schema to metadata is red is exactly
the shape of a gap that survives a review.

The fix is the one line the diagnosis predicted: `env.py` now imports
`end_user_session_tables` beside its sibling. `alembic check` is clean, and
stays clean across the 0034→0029→head round trip.

**The lesson is the missing guard, not the missing line.** Nothing fails when a
new table module is added and its import is forgotten — except `alembic check`,
which is the last thing anybody runs. A test asserting that every module under
`*/infrastructure/*tables.py` is reachable from `Base.metadata` would have
caught this at the moment it was introduced. That test does not exist and this
run did not write it.

**A session whose issuer predates migration `20260820_0034` has its writes
refused outright, not degraded to the old behavior.** `channel_issuer_id` is
nullable exactly because a session written before that column existed carries
no value for it, and `EndUserIdentityService.allowed_origins_for_issuer`
treats `None` there as "this session's origin cannot be verified" — it returns
an empty set rather than falling back to the pre-fix union across every active
issuer in the workspace. The practical consequence: everybody who was signed
in across the moment this deployment ran migration 0034 has every
state-changing request refused (`cross_origin_forbidden`, since their own
origin can never be in an empty allowed set) until they sign in again and get
a session that does carry an issuer. Nothing in this run exercised that
migration boundary against a database holding real pre-0034 sessions — the
behavior is proven by unit test against the domain function
(`allowed_origins_for_issuer`'s own docstring and the design decision behind
it), not by bringing up a database with sessions minted before 0034 and
watching a write actually get refused end to end.

**The neither-`Origin`-nor-`Referer` gap is proven as "lets the request
through," not bounded to the population it is meant to cover.**
`test_a_write_with_no_origin_or_referer_still_succeeds` proves the code does
what `enforce_end_user_origin`'s docstring says it does. What no test in this
run measures is *how often* a real cross-site browser write actually arrives
with neither header — the design's own argument is that a browser always
sends at least one on a cross-site fetch/XHR regardless of `Referrer-Policy`,
which is sound, and says nothing about a non-browser client replaying a
captured session cookie directly against the API with no `Origin` header at
all. Nothing stops that today except that the credential (and therefore the
cookie behind it) is meant to live only inside a browser tab the enterprise's
page opened.

**Only the `public_key` issuer path was exercised end to end.**
`tests/e2e/end-user.spec.ts` registers its issuer with a PEM `public_key`, not
a `jwks_url`. The `jwks_url` path is proven separately and for real —
`tests/integration/identity/` has a test that resolves a `channel_issuers`
row's `jwks_url` through a live `EgressProxy` on a real socket
(commit `ac19835`), not a fake — but nothing in this run drives a browser
through a chat surface backed by a rotating-key issuer.

**No end user in this run ever touched a sandboxed tool.** The e2e Agent
publishes with `tools: []`; §5's Run wiring reuses the same Worker, sandbox and
checkpoint machinery every other caller type already goes through, and nothing
here suggests that path treats `caller_type=end_user` any differently — but
this run has no direct evidence of an end user's Run calling `shell.exec` or
an HTTP/MCP tool, only evidence that Runs of every other kind already do.

**The recurring Minor from tasks 1 through 4: TDD's red-first discipline is
not verifiable from git history for most of this branch.** `progress.md`
records this against each of the first four tasks — test and implementation
landed in the same commit, so there is no red commit to point at as evidence
the test would have failed first. Tasks 5 through 7 split test and
implementation commits (visible in §1's table: `test(...)` commits ahead of
their implementation), which is why task 5's own note says "本轮起测试与实现
分开提交，前四轮无法验证的「红先行」这次可查" — the later tasks are
checkable, the earlier ones are not, and this record does not manufacture
verification that was not done at the time.

**Task 6's negative case has no test.** `progress.md` records it plainly: no
test pins that reading an *ordinary member's* own session — not an end user's
— writes no audit row. Reviewed by code inspection, not proven by a test that
would fail if a future change started auditing every read indiscriminately.

**`packages/backend/tests/integration/sandbox/` fails on this host, unrelated
to this work.** `test_transport.py` and `test_transport_streaming.py` give 2
failures and 15 errors, all `OSError: AF_UNIX path too long` — this machine's
socket path (under this session's scratch/checkout directory) exceeds the
108-byte `sockaddr_un` limit macOS enforces, which has nothing to do with the
sandbox transport code itself. This is the same failure class M2E's record
documented on this same host, and it is excluded from §3's integration count
rather than folded into it.

**Two of M2E's own unresolved items still apply unchanged, because nothing in
this task touched the code they describe:** HTTP and MCP tools still cannot be
delegated to a child Agent (`AgentCatalog._check_delegation` and
`scope_of_spec` are unmodified by this branch — confirmed by `git diff --stat`
against `530ba97` showing no touch to `agents/application/service.py`'s
delegation-checking functions beyond the two-gate additions elsewhere in that
file), and §24.1's performance gate was not re-run here at all; this task's
brief does not ask for it and the M2 roadmap already marks that gate settled
as of the M2E record.

**Nothing here was run against a real model.** Every scenario — the e2e walk's
Agent, every integration test's Run — uses the deterministic provider. Whether
a real enterprise's signing service, integrated by a real developer reading
`docs/development.md`'s new section, produces a credential this platform
accepts on the first try is a question about a system this run did not touch.

## 6. What this stage does not claim

**Cross-channel identity is not merged.** The same person arriving through
Feishu and through this chat surface is two separate `EndUser` rows with two
separate sets of memories — `external_identities`'s uniqueness is
`(workspace_id, channel, external_user_id)`, not `(workspace_id,
external_user_id)`, so nothing in the schema even has a place to record "these
two rows are the same human." Design §10 names this a deliberate, unbuilt
feature; §282 requires the two-way binding this would need, and this design
provides no door for it at all — not a partial one, not one with a manual
workaround.

**The platform trusts a signature, not a fact.** `verify()` proves an
enterprise possesses the private key registered against an issuer and used it
to sign a claim about who `sub` is. It never asks, and has no way to ask,
whether the enterprise's own signing service actually verified that person's
identity before writing that claim. An enterprise that mistakenly (or
maliciously) signs a credential asserting `sub=someone-who-does-not-work-here`
produces a token this platform accepts exactly as readily as a correct one —
the entire trust boundary is "this key holder says so," and §4.5.1's line that
the platform is not an identity provider cuts both ways: it also never
double-checks the one that is.

**Disabling an issuer does not end sessions already exchanged from it.**
`ChannelIssuerStatus.DISABLED` is checked at `verify()` time, which only ever
runs during a credential exchange — an `end_user_sessions` row minted before
the disable is untouched by it, and stays valid until it expires (8 hours by
default) or is separately revoked with `DELETE
/api/v1/end-user/sessions/{end_user_id}`. This is §4.3's own documented trade,
re-pinned by `test_disabling_an_issuer_refuses_new_credentials_but_not_the_already_exchanged_session`
as a known behavior rather than a bug to fix quietly — "disable" and "log
everyone out right now" are two different admin actions with two different
endpoints on purpose, because folding session revocation into the
issuer-status check would mean every ordinary request pays the cost of
re-checking issuer state rather than just every credential exchange.

**HTTP and MCP tools still cannot be delegated to a child Agent.** Unchanged
from M2E: the `tools` face `AgentCatalog._check_delegation` matches is built
from `spec.tools`, which does not carry the generated `http.<tool>.<operation>`
or `mcp.<server>.<tool>` names the catalog resolves at publish time. A
delegated child's granted HTTP/MCP operations are always empty — fail closed,
not fail open — and completing this remains a change to
`AgentCatalog._check_delegation` that no task in this plan made, because none
of them touch delegation at all.

**An end user is a subject the platform runs work for, not a person it has
opinions about.** `end_users` carries no name, no email, no anything an
enterprise's own directory already owns — `test_end_users_columns_carry_no_identifiable_information`
pins that as a schema-level promise, not a convention developers are trusted
to keep by hand. Whatever an enterprise chooses to put in a credential lands
on `external_identities.profile`, scoped to one channel, and erasure
(`erased_at` on `end_users`) does not have to chase that information through
every table that ever touched this subject — because none of them hold it in
the first place.

**A developer's "view" is read-only, and that boundary is enforced, not
requested.** §4.6 opened exactly one capability — reading an end user's
session content — and `test_a_developer_cannot_correct_forget_or_erase_an_end_users_memory`
is the other half of that sentence: correct, delete and forget stay closed to
a developer regardless of how the read path is reached. A developer who wants
those has to be the end user, through the end user's own self-service door,
which a developer's platform session cannot open.
