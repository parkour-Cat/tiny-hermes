# M1 phase 3C session workspace verification — 2026-08-13

## 1. Scope

This record covers the M1 SessionWorkspace slice described by
`docs/superpowers/plans/2026-08-11-m1-session-workspace.md`: persistent
session files as immutable MinIO revisions, safe `file.*` tools over an
`openat2` helper, a framed streaming subprotocol on the Controller socket, the
five-table checkpoint commit, over-limit rollback with the guarded
`interrupted -> paused(limit)` door, Artifacts with tenant-authorized
download, Scheduler garbage collection, and the credential boundary asserted
in the running stack.

The implementation commits:

| Commit | Result |
|---|---|
| `ecfeb96` | Define deterministic workspace manifests. |
| `72949ca` | Bounded MinIO object adapter behind the ObjectStore port. |
| `c99cb91` | Workspace, upload, and artifact storage schema (migration 0007). |
| `28ab09b` | Recoverable ObjectUpload lifecycle and the GC planner. |
| `8697b21` | Framed streaming subprotocol (pure codec and receiver). |
| `1e67026` | Kernel-bounded cache tmpfs; `disk_mb` removed; volume labels. |
| `b93cfb0` | Static `openat2` file helper compiled into the runtime image. |
| `5f75298` | Engine volumes, tar import/export/scan, streamed exec. |
| `b3a0708` | Controller workspace actions and the framed negotiation. |
| `2933042` | SessionWorkspace restore/checkpoint and the atomic committer. |
| `369c00d` | `file.list` / `file.read` / `file.write` tool bindings. |
| `6f5fa3a`, `7f1b9bd` | The one narrow limit-pause door and its intent guard. |
| `c88587e`, `b4748bb` | stdin over frames; the bound port with no-disk demux. |
| `259e7c1` | Worker restore, per-round checkpoint, rollback, honest pause. |
| `105eadb` | Artifacts: recorder, stores, tenant-authorized routes. |
| `a0ce611` | Scheduler GC: upload reclamation and artifact retention. |
| `3fd0be7`, `cefc969` | Credential boundaries, CI wiring, and the drill. |

## 2. Environment

Development moved mid-slice: Tasks 1–2 were built on the Windows machine that
carried phases 1–3B, whose Docker Desktop then failed (a disk-cleanup tool had
removed the WSL MSI and `C:\Windows\Installer` cache — repaired, but the local
stack stayed unavailable). Verification from Task 3 onward ran on a Linux
machine (kernel 6.12, Docker 29.1.3 with the vfs storage driver, PostgreSQL
17.6, MinIO RELEASE.2025-07-23T15-54-02Z, Python 3.12, uv 0.11.26), and on
GitHub's Ubuntu runners.

## 3. What was verified by execution

**Unit, 547 tests.** Manifest determinism (NFC, bytewise order, privilege-bit
stripping, duplicate refusal), quota arithmetic including replacement deltas,
the frame codec and receiver (sequence gaps, credit as non-terminal
backpressure, declared totals, digests, idle deadlines as decisions), the
container policy's tmpfs cache and ownership labels, the tool registry's
two-step authorization with hostile paths refused generically, the state
machine's one new door, the tar demux, artifact authorization where a
cross-tenant probe is indistinguishable from a missing artifact, and the
archive-extraction bans (linter, source walk, and a `tarfile` import
allowlist).

**Integration against real PostgreSQL and MinIO.** Migration 0007/0008
round-trips (fail-for-the-right-reason first, then up/down/up); the upload
lifecycle's guarded transitions raced across sessions; GC-root protection with
a 400-run collector pass; the five-table commit: two candidates from one base
where exactly one advances, a poisoned mid-transaction refusal that moves
nothing, a lost answer reconciled by `upload_id` without a double commit; the
worker flows end to end with a gateway-fake sandbox — restore before the first
model call, checkpoint before the next one, over-quota rollback into
`paused(limit)` with the intent cleared in the same transition, unconfirmed
destroy staying `interrupted`, conflict becoming `failed`; artifact recording
with ceilings enforced while bytes arrive; and the scheduler's upload and
retention sweeps, including a blob kept because a retained manifest references
it.

**Against a real Docker daemon.** The `openat2` helper: ten tests including a
400-iteration symlink-swap race with zero reads escaping the data root; cache
tmpfs `ENOSPC` at both byte and inode ceilings while `/workspace/data`
accepts; import/scan/export round-trips with digests; streamed exec draining
8 MiB past a 1 MiB cap without blocking; stdin fed whole through the exec
socket to the helper's atomic write; and the full 3B sandbox suite unchanged.

## 4. What is asserted rather than executed locally

The compose stack could not run on the Linux development machine (nested
Docker forwarded no inter-container TCP), so everything stack-shaped is CI's
to prove, exactly as the restart drill already was: the credential boundary
(`S3_*` empty in the controller, absent Docker socket in the worker), both
drills, and the Playwright suite. The workspace drill's performance gates are
end-to-end envelopes over the public API — the commit itself is not separable
there — with raw timings printed into the CI log for this record's successor
to tighten.

## 5. CI evidence

The full-suite run for this slice lives on the branch's Actions history
(`ci` workflow, branch `m1-sandbox`). `backend-integration` runs every test
above against service containers, and `compose-e2e` runs the boundary
assertions and both drills.

A later Actions run (`31693609570`) showed the quota drill hanging after the
checkpoint itself was already correct (`status=limit_exceeded`,
`total_bytes=12582916`, quota 8 MiB). The Worker had recorded `INTERRUPTED`
(releasing the lease) and then called `destroy`, which the Controller refused
as `lease_invalid`. The drill treated that staging state as the outcome and
opened the SSE history of a non-terminal Run, which never closes. The Worker
now reclaims with the no-lease `cleanup` action after the rollback
transaction, and the drill waits only for `paused` / `completed` / `failed`.

## 6. Standing limitations, stated plainly

- **The checkpoint quota is not a host-disk hard quota.** A malicious command
  can temporarily fill an ordinary named volume before its post-command scan;
  what the platform guarantees is that the excess never becomes a committed
  revision and that the one over-limit step rolls back (design §9, A1).
- The artifact recorder is not yet threaded through `shell.exec`'s streamed
  execution: long inline output is still truncated with a marker, and the
  drill's artifact-download scenario waits on that wiring (backlog, with the
  volume-orphan label sweep).
- An unsupported workspace entry interrupts the Run for recovery rather than
  reacquiring mid-slice; a timed-out command checkpoints its frozen state.
  Both are documented simplifications against §10/§16.3's letter.

## 7. Phase 3C completion checklist

- [x] File tools pass both authorization checks and cannot escape the data
      root, including under a symlink race.
- [x] A committed revision survives Worker, container, and process restarts
      (integration; the drill repeats it against the stack in CI).
- [x] Session pointer, Run checkpoint, tool turn, event, and upload status
      move in one transaction; concurrent base revisions cannot overwrite
      each other.
- [x] Exactly-at-limit commits succeed; one unit over refuses (unit-level
      quota arithmetic; integration rollback path).
- [x] A1 rolls back only the over-limit round; `interrupted -> paused` is
      reachable only with the recorded limit-pause intent.
- [x] Cache bytes and inodes are physically bounded by tmpfs (`ENOSPC`
      observed against a real daemon).
- [x] Long output produces a tenant-authorized Artifact with explicit
      truncation (recorder + routes); the preview-keeping tool result waits on
      the backlog wiring above.
- [x] The Controller has no MinIO credential; the Worker has no Docker socket —
      asserted in the running stack by `compose-e2e`.
- [x] Scheduler cleans staging rows, candidate indexes, and expired Artifacts
      without a whole-bucket scan; failures are reported, not marked done.
      (The volume sweep is teardown-integrated; label enumeration is backlog.)
- [x] Performance gates recorded as end-to-end envelopes with raw timings in
      the drill output.
- [x] Every phase 1–3B check passes unchanged or its deliberate replacement is
      documented (the 3B cleanup audit gained the volume entry; the agents
      test's unimplemented-tool example moved off `file.read`).
- [x] This record states the checkpoint-quota limitation plainly (§6).
