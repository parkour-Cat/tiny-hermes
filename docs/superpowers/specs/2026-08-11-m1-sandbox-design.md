# M1 Sandbox Design

> Date: 2026-08-11
>
> Status: written design for user review
>
> Delivery slice: M1 phase 3B of three

## 1. Purpose and authority

Phase 3A gave the platform a real model and a guarded way to reach it. The model
can now say things. It still cannot *do* anything: `AgentSpec.tools` is `()`, the
adapter tells the model about no tools, and a model that asks for one gets a
normalized refusal.

This slice builds the place where doing things is allowed to happen — a Docker
sandbox the platform owns, that the Agent cannot configure, that has no network,
and that cannot be escaped into the host — and it gives that sandbox exactly one
tool, so the whole apparatus has a caller from the day it exists.

The following documents remain authoritative:

- `docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` v2.4,
  especially §16 (tools, approval, sandbox) and §12.3's state guarantees;
- `docs/superpowers/specs/2026-08-09-tiny-hermes-m1-technical-design.md` v1.1,
  especially §10 (tools), §11 (Sandbox Controller), and §6.4 (sandbox tables);
- `docs/superpowers/plans/2026-08-10-tiny-hermes-m1-roadmap.md` phase three;
- the phase-3A design for the provider seam and the transcript this slice extends.

Three rules from those documents govern this slice above everything else:

- **产品设计 §16: 沙箱失败时不得回退宿主机执行.** There is no fallback path. A
  sandbox that will not start fails the Run.
- **产品设计 §16: Docker 控制权只授予可信的 sandbox-controller. API、Web、Agent
  沙箱和模型均不得直接访问 Docker socket.** This is a process boundary, not a
  code convention.
- **技术设计 §11.1: 仅能连接 Unix socket 不能替代这些校验.** Reaching the
  Controller is not authorization. Every call re-checks Reservation ownership and
  lease validity.

## 2. The slice was re-cut, and this is why

The roadmap's phase three is one block; §1 of the phase-3A design split it. That
split put the sandbox in 3B and the tools in 3C. **Building it that way would
produce a Sandbox Controller with no caller.**

Technical design §11.4 is explicit that a sandbox is acquired only by a Run whose
Agent has tools bound — `没有工具绑定的 Run 不为此提前创建沙箱`. With no tool in
3B, no Run would ever acquire one, and the slice would end with a subsystem
exercised only by its own tests. This repository has refused that shape
everywhere else, and one of the roadmap's phase-three exit checks —
`同一时间片多工具步骤不重建容器` — cannot even be stated without tools.

So the line moves:

| | |
|---|---|
| **3B** (this slice) | The Sandbox Controller, container policy, reservations and lifecycle, Scheduler reclamation, two-step authorization, and **`shell.exec`** — the one tool that proves all of it. |
| **3C** | `file.list`, `file.read`, `file.write`; `/workspace/data` revision CAS; ObjectUpload staging; Artifact. |

`shell.exec` is the right one to bring forward: it needs the container and
nothing else. The file tools need `/workspace/data` to be a durable, versioned,
object-store-backed thing, which is a second large subsystem and properly 3C's.

## 3. Observable outcome

After phase 3B, a developer can publish an Agent bound to `shell.exec` and watch
it work:

1. A platform administrator sees the approved runtime image digest in
   configuration, and the Controller refuses any other.
2. A developer binds `shell.exec` to an Agent at publish time and submits a Run.
3. The model asks to run a command; the platform executes it inside a container
   with no network, no root, a read-only root filesystem, and a resource ceiling;
   the output comes back as a tool result and the model continues from it.
4. Several commands in one execution slice reuse the same container — provable
   by asking the sandbox for its own boot id and getting the same answer twice.
5. The Run crosses a slice boundary: the container is frozen, the lease is
   released, and the next slice thaws the same instance and reports
   `cache_state=reused`.
6. After `sandbox_idle_ttl`, or on a new Run in the same Session, the Agent gets
   a fresh writable layer and `cache_state=reset` — and is *told* so through a
   protected runtime hint and a `sandbox_cache_reset` event, rather than
   discovering it by finding its virtualenv gone.
7. A command that outruns its timeout returns `command_timeout` as a tool result,
   and the loop decides what to do next; it does not fail the Run by itself.
8. `curl https://example.com` inside the sandbox fails, because the container has
   no network — not because a policy says no.
9. A Run that reaches `paused`, `waiting_*`, or a terminal state leaves no
   container and no reservation behind.
10. A Worker killed mid-command leaves a container that the Scheduler isolates
    and destroys after the lease expires, and the Run becomes `interrupted`
    rather than silently requeued.

And an operator can confirm from `docker ps` that nothing runs as root, nothing
has a network, and no container outlives the Run that owns it.

## 4. Explicit non-goals

- **No file tools and no `/workspace/data` durability.** `/workspace/data` exists
  as a mount in 3B so `shell.exec` has a working directory, but nothing versions
  it, nothing uploads it, and it does not survive the SandboxInstance. Saying
  otherwise would be the platform claiming persistence it does not have. 3C.
- **No Artifacts.** A command that produces more output than the message limit
  gets its output truncated with an explicit marker; storing the remainder is 3C.
- **No network in the sandbox, and no egress proxy.** Product design §16 defers
  MCP, OpenAPI, and networked tools to M2 behind a dedicated `egress-proxy`. The
  container is created with `network_mode=none`, so a tool that ignored policy
  still could not reach anything.
- **No approval system.** Product design §16 states the M1 precondition: tools
  are bound at publish, act only inside the caller's own workspace, and requests
  outside those bounds are refused rather than queued for approval.
- **No sub-agents, no parent/child task tree.**
- **No snapshot store and no dependency pre-building.** §11.3: the only shared
  layer is the platform's own runtime image, keyed by immutable digest.
- **No resource profile beyond one.** §11.2's default profile is the only one;
  choosing among profiles needs a second one to exist.

## 5. Processes and the trust boundary

Compose gains a fourth long-running service.

```text
                    ┌─────────────┐
   docker.sock ───► │ controller  │ ──► containers (no network, non-root)
                    └─────────────┘
                           ▲
                    controller.sock  (a shared volume, nothing else)
                           │
              ┌────────────┴────────────┐
          ┌───────┐                ┌───────────┐
          │worker │                │ scheduler │
          └───────┘                └───────────┘

   api, web ──── no socket of either kind
```

**Only the Controller mounts `/var/run/docker.sock`.** The Worker and the
Scheduler mount a shared volume holding the Controller's Unix domain socket, and
nothing else. The API and Web containers get neither. This is what product design
§16 means by `Docker 控制权只授予可信的 sandbox-controller`, and it is enforced
by the Compose file rather than by anybody's discipline: a Worker that wanted to
create a privileged container has no socket to ask.

The Controller is deliberately small. It answers six actions, generates every
Docker parameter itself from the database, and accepts no host path, no
capability, no image, no mount, and no network mode from its caller.

### 5.1 Reaching the Controller is not authorization

§11.1 says this outright and it is worth restating, because a Unix socket in a
shared volume *feels* like a boundary. It is not one: both the Worker and the
Scheduler can reach it, and they may act on different Runs. Every call therefore
re-checks:

- the `SandboxReservation` for `run_id + sandbox_id` exists and belongs to that
  Run;
- for a Worker call, the `worker_lease_id` belongs to the same Run, has not
  expired, and has not been released;
- for a Scheduler call, the caller presents the separate `sandbox.cleanup`
  authority, the Run's lease has actually expired, and an AuditEvent is written.

`acquire` additionally checks that the Run holds no other live Reservation.

## 6. The Controller's interface

Six actions, per §11.1:

| Action | Meaning |
|---|---|
| `acquire` | Create a writable layer over the approved image, or thaw this Run's kept instance. Answers `sandbox_id` and `cache_state`. |
| `execute` | Run one tool request inside the instance. |
| `freeze` | Stop the instance from getting CPU, at a slice boundary. |
| `thaw` | Resume it for a new slice. |
| `destroy` | Remove the instance and end the reservation. |
| `inspect` | Report the instance's state without changing it. |

### 6.1 The core is transport-free

`SandboxController` is a plain class with those six methods. It takes a Docker
client and a store, performs the ownership checks, and returns dataclasses. Every
rule in this document is testable by calling it directly.

A thin server exposes it over a Unix domain socket with newline-delimited JSON,
and a thin client speaks that. Neither contains a rule.

This split is not only tidiness. `socket.AF_UNIX` does not exist in Python on
Windows — verified on the development machine, where `hasattr(socket, "AF_UNIX")`
is `False` — so a transport test cannot run there at all. Putting every rule
behind the transport would mean the entire Controller was untestable on the
machine it is being written on. Instead:

- the rules are tested in-process, on any platform, against a real Docker daemon;
- the transport gets its own small test, skipped on Windows with an explicit
  reason, and covered in CI.

**This is a real gap and it is stated rather than smoothed over.** Until CI runs,
the UDS transport is the one part of 3B verified by reading rather than by
execution. §14.4 says what is done about that.

### 6.2 Why not HTTP over the socket

The phase-3A ban forbids constructing an HTTP client outside
`tiny_hermes.outbound`, and §15 of the 3A design says the Controller must not use
that module: it is built to treat addresses as hostile, and the Docker socket is
a trusted local seam with entirely different rules. Rather than weaken the ban
with a second exemption, the Controller protocol uses
`asyncio.open_unix_connection`, which cannot reach a network at all.

## 7. Container policy

Every parameter comes from the platform. §11.2, in the form the Controller
actually passes:

| Setting | Value |
|---|---|
| image | the approved digest, from configuration; never from the Agent |
| user | a non-root uid baked into the image |
| `read_only` | `True` — the root filesystem is not writable |
| `network_mode` | `none` |
| `cap_drop` | `ALL` |
| `security_opt` | `no-new-privileges:true` |
| cpu / memory | 1 vCPU, 1 GiB |
| pids limit | 128 |
| writable layer | 2 GiB |
| `/tmp` | tmpfs, 256 MiB, `noexec,nosuid,nodev` |
| mounts | this Run's `/workspace/data` and `/workspace/cache` volumes, nothing else |
| init | `True`, so a zombie-producing command does not accumulate processes |

An `AgentVersion` may select a profile no larger than the instance ceiling. In
3B there is exactly one profile, so the check has one value to compare against —
written anyway, because the second profile must not be the moment the rule is
invented.

The Controller refuses an image digest that is not in the approved list. The
image itself is built by this repository (`deploy/sandbox/Dockerfile`), pinned by
digest in configuration, and pulled rather than built at run time.

## 8. Lifecycle, and what the Agent is told about it

§11.4's table, and one rule from product design §16 that is easy to lose:

> `running → queued` 的执行时间片边界只释放 WorkerLease。释放前
> sandbox-controller 必须冻结 SandboxInstance… 冻结失败时 Run 进入
> `interrupted`，不得伪装成已重新排队。

So the slice boundary is: freeze, and only then release the lease. A freeze that
fails is not a slice boundary — it is an `interrupted` Run. Likewise §12.3:
entering `paused`, `waiting_*`, or a terminal state requires the instance to be
*destroyed* first, and a destroy whose result is unclear is `interrupted` rather
than a pause that quietly leaves a container running.

`cache_state` is the Agent-visible half:

- `reused` only when this same Run thaws its own instance inside
  `sandbox_idle_ttl`;
- `reset` for every new instance, **including the next Run in the same Session**.

On `reset` the Worker writes a `sandbox_cache_reset` event and prepends a
protected runtime hint to that slice's first model call, saying plainly that
dependencies, virtualenvs, background processes and build caches are gone and
must be rebuilt. Protected means later conversation cannot override it.

It does **not** write a marker file into `/workspace/data`. §11.3 forbids it, and
the reason is worth keeping: a file the Agent did not create, in the directory
the Agent believes is its own, is the platform lying in the Agent's own
workspace.

## 9. Data model

Two tables, per §6.4, in one migration.

`sandbox_reservations`: `id`, `run_id`, `workspace_id`, `sandbox_instance_id`,
`status` (`active`, `kept`, `isolated`, `released`), `idle_expires_at`,
`isolation_reason`, timestamps. Unique partial index on `run_id` where the
status is live, so `acquire`'s "no other live Reservation" rule is the database's
rule and not a race.

`sandbox_instances`: `id`, `container_id` (the Controller's own handle),
`image_digest`, `status` (`running`, `frozen`, `destroyed`, `isolated`),
`resource_profile`, `boot_id`, timestamps. **No host path column**, asserted by a
test: §6.4 says `不保存任意宿主机路径`, and a column that could hold one is a
column somebody will eventually put one in.

## 10. The tool seam

### 10.1 Two-step authorization

§10.2, and both steps are real with one tool:

1. **Before the schema reaches the model.** `ToolRegistry` returns the tools this
   AgentVersion bound, filtered by workspace and Run policy. An Agent with no
   `shell.exec` binding has it in no schema the model ever sees.
2. **Before execution, against the real arguments.** The tool name, the working
   directory, the timeout and the resource ceiling are checked again. A `cwd`
   outside the allowed workspace directories is `tool_not_authorized`, and the
   underlying implementation is not called.

An unbound tool the model asks for anyway returns `tool_not_authorized` as a tool
result. This is the one place phase 3A's `tool_use_not_supported` failure goes
away: a bound tool is now a thing the platform can answer.

### 10.2 `shell.exec`

Request: `command`, `cwd`, optional `timeout_seconds`. Executed by
`/bin/bash -lc` inside the instance, as the non-root user, with no host
environment and no Docker arguments reachable from the request.

Limits per call: a timeout (60s default), an output byte cap (1 MiB into the
message), and the container's own pids and disk ceilings. Output beyond the cap
is truncated with an explicit note saying so — in 3B the remainder is discarded,
because storing it needs the Artifact machinery 3C brings. A truncation that
pretends to be a whole answer would be worse than a short one.

A timeout returns `command_timeout` as a tool *result*, not a Run failure: §11.5
puts that decision in the loop.

### 10.3 CanonicalMessage widens

Phase 3A left this deliberately: `CanonicalMessage.text` is a single string, with
a docstring saying the widening belongs to the slice that has a producer. This is
that slice.

```python
Block = TextBlock | ToolCallBlock | ToolResultBlock
```

Every existing `match` and every reader of `.text` will fail typechecking, which
is the review the widening was deferred to get. The stored document already
carries a `parts` list, so rows written in 3A read back unchanged.

The adapter's `tool_calls` handling stops being a refusal and starts being a
parse.

## 11. Failure mapping

§11.5, plus where each one is decided:

| Failure | Result | Decided by |
|---|---|---|
| Start fails, confirmably, before the container exists | bounded retry, then `failed` with `sandbox_start_failed` | Controller reports; Worker retries |
| Command times out | `command_timeout` tool result | Controller; the loop decides next |
| Connection lost mid-execute | `interrupted` | Worker |
| Freeze fails at a slice boundary | `interrupted` — never a quiet requeue | Worker |
| Destroy result unclear | `interrupted`, instance marked `isolated` | Worker or Scheduler |
| Disk or file-count ceiling reached | safe checkpoint, then `paused(limit)` | Worker |
| Lease expired with a live instance | isolate, then freeze or destroy, then decide recovery | Scheduler |

There is no host-execution fallback anywhere in that table, and a test asserts
the absence: no code path outside the Controller may start a process, which is
enforced the way the HTTP ban is — `subprocess`, `os.system`, and
`asyncio.create_subprocess_*` become banned APIs outside the Controller module.

## 12. Configuration

| Setting | Default | Notes |
|---|---:|---|
| `sandbox_controller_socket` | `/run/tiny-hermes/controller.sock` | the shared volume path |
| `sandbox_image_digest` | (required when tools are enabled) | approved digest, allowlist of one |
| `sandbox_idle_ttl_seconds` | 300 | §11.4; 0–1800, workspace may only lower |
| `sandbox_cpu_limit` | 1.0 | |
| `sandbox_memory_mb` | 1024 | |
| `sandbox_pids_limit` | 128 | |
| `sandbox_disk_mb` | 2048 | |
| `sandbox_tmp_mb` | 256 | |
| `shell_timeout_seconds` | 60 | per call ceiling |
| `shell_output_bytes` | 1048576 | into the message |
| `sandbox_start_attempts` | 3 | |

## 13. Verification strategy

### 13.1 Rules, against a real Docker daemon, in-process

The Controller's checks and the container policy are tested by calling
`SandboxController` directly, with a real Docker client, on the developer's
machine and in CI. These are the tests that matter:

- a container is created non-root, read-only, with `network_mode=none`, all caps
  dropped, and the resource ceilings — asserted by *inspecting the created
  container*, not by inspecting the arguments we passed;
- `curl` and a raw socket connect from inside the container both fail;
- a write to `/` fails and a write to `/workspace/data` succeeds;
- `acquire` twice for one Run without releasing is refused;
- a Worker call carrying another Run's lease is refused, and nothing is created;
- a Scheduler cleanup call against a Run whose lease is still live is refused;
- an image digest outside the allowlist is refused;
- `execute` after `freeze` is refused;
- two `execute` calls in one slice see the same `boot_id`.

### 13.2 Lifecycle, against PostgreSQL and Docker

Reservation states, `cache_state=reused` inside the TTL and `reset` after it, the
next Run in the same Session getting `reset`, freeze-then-release ordering, and
the Scheduler destroying a kept instance after expiry.

### 13.3 A Run that runs a command

The whole path, as an integration test: publish an Agent bound to `shell.exec`,
submit a Run, let the Worker drive it, and assert the model received a tool
result containing the command's real output — with the model played by a
recording provider that asks for a specific command, because the point is the
platform's behavior and not a real model's choices.

### 13.4 The transport, and the gap

One test drives the UDS server and client, skipped on Windows with the reason in
the skip message. **On the development machine this slice therefore has one
component verified by reading.** Two things reduce that:

- the client and server contain no rules, so what is unverified locally is
  framing, not policy;
- CI must run before 3B is called done. This is the point at which the standing
  "CI has never run" note stops being deferrable: 3B needs Docker-in-Docker in
  the `compose-e2e` job, and a GitHub runner differs from Docker Desktop on
  cgroups, seccomp defaults, and overlayfs in ways that nothing local will
  surface.

### 13.5 The restart drill gains a fourth scenario

A Worker killed while a command is running, with a container live. Assert the
Scheduler isolates and destroys it, the Run becomes `interrupted` and then
recovers, and `docker ps` is clean afterwards. The drill already proves no
committed state is lost; this proves no container is leaked.

## 14. Deliberate deferrals

1. **`/workspace/data` is a mount, not a persistent workspace** (§4). Versioning
   and object storage are 3C.
2. **Truncated command output is discarded** (§10.2), because keeping it needs
   Artifacts.
3. **One resource profile** (§7).
4. **No egress proxy** (§4); the sandbox has no network at all, which is
   stronger than a proxy and is what M1 promises.
5. **The UDS transport is CI-verified only** (§13.4).

## 15. Exit criteria

- [ ] A published Agent bound to `shell.exec` completes a Run whose model saw
      real command output.
- [ ] The created container is non-root, read-only, network-less, all caps
      dropped, and within every ceiling — asserted from `inspect`, not from the
      call.
- [ ] Nothing inside the sandbox can reach the network.
- [ ] An unbound tool is refused by the execution layer even when the model asks
      for it correctly.
- [ ] A `cwd` outside the allowed directories is refused before the underlying
      implementation runs.
- [ ] Two tool steps in one slice share one container; a new Run does not.
- [ ] `cache_state=reset` produces an event and a protected runtime hint, and no
      marker file in `/workspace/data`.
- [ ] A failed freeze produces `interrupted`, never a requeue.
- [ ] `paused`, `waiting_*`, and terminal states leave no container and no
      reservation.
- [ ] A killed Worker leaks no container: the Scheduler isolates and destroys it.
- [ ] No code outside the Controller module may start a process, enforced by
      lint and proven by a test.
- [ ] `sandbox_instances` has no host path column.
- [ ] **CI has run, including Docker-in-Docker.**
- [ ] Every phase 1, 2A, 2B, 2C, and 3A check passes unchanged.

### Next seams

Phase 3C consumes: the Controller's `execute`, which the file tools call with
different requests; the two-step authorization, which gains three more tool
names; and `/workspace/data`, which stops being a scratch mount and becomes a
revisioned workspace with CAS at every checkpoint that may have written to it.
