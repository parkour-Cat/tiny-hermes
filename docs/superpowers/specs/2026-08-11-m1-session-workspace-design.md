# tiny-hermes M1 Phase 3C SessionWorkspace, file tools, and artifacts

> Date: 2026-08-11
>
> Status: confirmed design; implementation has not started
>
> Inputs: product design v2.4, M1 technical design v1.1, phase-3B sandbox design

## 1. Why this slice exists

Phase 3B proved that an Agent can run `shell.exec` only inside its own locked-down
Docker sandbox. It deliberately left `/workspace/data` as a Run-owned scratch
volume: there is no durable Session workspace, no file tool, and no Artifact.

The same work exposed one honest gap. The default resource profile carries a
2 GiB `disk_mb` value, but Docker's ordinary named volumes do not enforce that
number. `storage_opt` limits the container writable layer, not the two volumes
where this project writes, and it is not portable across Docker Desktop, hosted
CI, and ordinary Linux filesystems.

Phase 3C therefore delivers two different and deliberately named controls:

1. **A checkpoint quota for `/workspace/data`.** Content over the quota cannot
   become a committed SessionWorkspace revision. This protects product state,
   but it is not advertised as a physical host-disk hard limit while an
   arbitrary command is still running.
2. **A physical tmpfs limit for `/workspace/cache`.** Cache is disposable by
   definition, so the kernel can enforce byte and filesystem-object ceilings
   without a persistence conflict.

This is the selected portable M1 design. XFS/ext4 project quotas may later be a
self-hosted Linux adapter, but are not an undeclared prerequisite for 0.1.

The storage choice is based on the platform contracts, not a guessed Docker
flag: Docker documents that tmpfs supports `size` and inode options and is
removed with the container, while per-container `storage_opt size` applies to
the container filesystem and, for overlay2, depends on an XFS backing
filesystem with `pquota`. Docker named volumes have no portable per-volume quota
contract. See [Docker tmpfs mounts](https://docs.docker.com/engine/storage/tmpfs/),
[Docker volume storage](https://docs.docker.com/engine/storage/volumes/),
[Docker `--storage-opt`](https://docs.docker.com/reference/cli/docker/container/run/#set-storage-driver-options-per-container---storage-opt),
and [Linux tmpfs](https://docs.kernel.org/filesystems/tmpfs.html).

## 2. Observable outcome

After 3C:

1. An Agent can use `file.list`, `file.read`, `file.write`, and `shell.exec`.
2. A successful write checkpoint creates an immutable SessionWorkspace revision
   in MinIO and PostgreSQL.
3. A new sandbox for the same Run, or the next Run in the same persistent
   Session, restores the last committed revision before the model sees tools.
4. Two submissions based on the same revision cannot overwrite one another.
5. A command that takes `/workspace/data` over quota loses only that command's
   uncommitted changes, preserves the preceding revision, and pauses the Run
   with an explicit limit event.
6. `/workspace/cache` survives freeze/thaw of the same warm SandboxInstance,
   but is empty after that instance is destroyed and is physically bounded.
7. Long command output is represented by a bounded preview and an authorized
   Artifact rather than being silently discarded.

## 3. Explicit non-goals

- No claim that `/workspace/data` cannot temporarily exceed its checkpoint quota
  during an arbitrary `shell.exec` call.
- No XFS/ext4 project-quota setup, loopback filesystem, privileged mount helper,
  host bind mount, or host path stored in platform data.
- No recovery of the one over-quota tool step. M1 restores the previous complete
  checkpoint; it does not retain an over-quota candidate for 24 hours.
- No persistence of `/workspace/cache`, `/tmp`, virtual environments, background
  processes, or open file descriptors.
- No Agent-built dependency image or writable layer shared between Runs.
- No general document editor, file sharing UI, archive extraction tool, or
  user-supplied object-store key.
- No symlink, device, FIFO, or Unix-socket entry in a committed M1 workspace.
  M1 persists ordinary files and directories only.

## 4. Terms and invariants

**SessionWorkspace** is the logical file space belonging to one Session. Its
current revision is `sessions.workspace_revision_id`; there is no second mutable
pointer elsewhere.

**WorkspaceRevision** is an immutable manifest. It names every ordinary file
and directory, each file's size and SHA-256 digest, total bytes, object count,
parent revision, and schema version. File bodies and the manifest document live
in MinIO; PostgreSQL stores the authoritative revision metadata and manifest
object reference.

**ObjectUpload** is the database registration for one server-generated staging
prefix. It is created before upload begins and becomes `ready`, `committed`,
`abandoned`, or `expired`. The Scheduler enumerates these rows rather than
scanning the whole bucket for guesses.

**Artifact** is a Run result that is not part of SessionWorkspace state. It has
its own limits, retention, and authorization.

The invariants are:

1. PostgreSQL never points at a revision whose manifest and referenced bodies
   have not already been uploaded and verified.
2. A revision commit compares the Session's current revision with the caller's
   base revision in the same transaction that advances the pointer.
3. A tool result that claims a write succeeded is committed atomically with the
   resulting workspace revision and Run checkpoint marker.
4. Restoring a revision verifies manifest hash, file size, and body hash before
   any model call or tool execution.
5. Object-store keys are generated from authenticated Workspace, Session, and
   server-side identifiers. A model or API caller never supplies a key.
6. No archive is extracted onto the API, Worker, Scheduler, or Controller host
   filesystem.
7. Every persisted ToolCallBlock has exactly one ToolResultBlock with the same
   call ID, including quota, conflict, timeout, unsupported-entry, and storage
   failure paths. Rollback changes the workspace revision, not transcript
   structural validity.

## 5. Module boundaries

### 5.1 SessionWorkspace module

This is the deep module that hides manifests, content hashing, MinIO staging,
deduplication inside one Session, revision comparison, and orphan registration.
Its application interface is deliberately small:

```python
restore(command: WorkspaceRestore) -> RestoreResult
checkpoint(command: WorkspaceCheckpoint) -> CheckpointResult
```

`restore` receives authenticated Workspace, Session, Run, lease, and sandbox
ownership, then locks or reads the Session's current revision itself. A caller
cannot choose an older revision while claiming to restore the current
workspace. `checkpoint` receives the same ownership plus the base revision and
applicable quota. Its result is one of `unchanged`, `committed`,
`limit_exceeded`, `conflict`, or `storage_failed`. Callers do not assemble object
keys or manipulate revision rows themselves.

The module depends on two internal adapters:

- a PostgreSQL repository plus MinIO object adapter;
- a `SandboxWorkspacePort` that streams a normalized workspace to or from the
  Controller without exposing Docker or a host path.

### 5.2 File-tool module

`file.list`, `file.read`, and `file.write` share one authorization and path
module. It normalizes a path, refuses absolute paths and `..`, refuses traversal
through a symlink, applies per-call size limits, and executes through the
Controller. `file.write` uses a same-directory temporary file and atomic rename.

Checking a path string and opening it later is not sufficient: a background
process could replace a checked directory with a symlink between those actions.
The sandbox image therefore contains a small file helper that opens the data
root once, resolves descendants with Linux `openat2`
`RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS|RESOLVE_NO_MAGICLINKS`, and performs reads,
writes, listing, temporary-file creation, fsync, and rename relative to held
directory descriptors. The Controller invokes only this fixed helper for
`file.*`; no user command or host path is interpolated into the call.

This module does not decide that a write is durable. The Worker reports success
to the transcript only after SessionWorkspace checkpoint succeeds.

The helper requires Linux `openat2`. Publishing or starting an AgentVersion that
binds any `file.*` tool runs a capability probe and fails closed with
`file_safety_unavailable` when the kernel or seccomp profile does not provide
the required resolution flags; there is no fallback to a
string-check-then-open sequence. A shell-only Agent does not expose the file
helper and is not rejected by this file-tool-specific probe.

### 5.3 Sandbox Controller

The Controller continues to own Docker and reservation/lease checks. 3C adds
restricted workspace import, metadata scan, content export, and named-volume
removal operations. Worker operations carry `run_id`, `lease_id`, and
`sandbox_id`, require a live matching lease, and recheck the existing ownership
invariant. Scheduler cleanup uses the already defined separate authority: no
live lease may exist, the Reservation must be isolated, and the Controller may
delete only the server-recorded container and volume IDs belonging to that Run.

The Controller never receives MinIO credentials. Workspace bytes are carried as
bounded, length-framed streams over the existing private Unix socket. Frames are
typed `start`, `data`, `progress`, `end`, `cancel`, or `error`; a frame carries at most
1 MiB, and the operation declares a server-checked total limit before its first
data frame. Every data frame has a monotonically increasing sequence and the
receiver advertises at most 8 MiB of unacknowledged credit, providing bounded
backpressure. The `end` frame carries total bytes, frame count, and a SHA-256
digest over the logical stream. Partial frames, a sequence gap, data after
`end`, digest mismatch, a total beyond the declared limit, or 30 seconds without
data or a `progress` frame causes cancellation. Workspace transfer defaults to a
five-minute total deadline and is configurable up to 30 minutes. Execute uses
the validated command timeout plus a 30-second drain grace, so a legal silent
15-minute command is not killed by the workspace-transfer deadline; the
Controller sends `progress` at least every 15 seconds while Docker is still
running it. There is no mid-stream resume: a workspace import restarts into a
fresh volume; an export retries under the same `upload_id` and skips already
verified content-addressed bodies. The Controller uses Docker archive operations
internally; neither caller can name a host path. The transport never buffers a
complete workspace in memory.

The current single-line JSON protocol remains for small control calls. Streaming
workspace and execute operations negotiate this framed subprotocol explicitly;
they do not base64-encode a workspace into the existing 64 KiB request line.

An interrupted import marks the sandbox dirty and requires confirmed container
and data-volume destruction before recovery. An interrupted export leaves the
workspace frozen, abandons or reconciles its registered upload, and makes the
Run `interrupted`; it is never treated as a complete checkpoint.

### 5.4 Worker and checkpoint committer

The Worker arranges the order: acquire, restore, model call, tool execution,
freeze, checkpoint, and then continue or release. It does not calculate quotas,
construct manifests, or choose object keys.

The Worker calls only the two SessionWorkspace operations in §5.1. An internal
checkpoint committer, not a second caller-facing interface, owns the PostgreSQL
transaction that touches the Workspace revision, Session pointer, Run
checkpoint marker, tool result, and RunEvent. This concentrates the cross-table
atomicity instead of spreading it through the Worker.

The preallocated `upload_id` is the commit idempotency key. If the database
answer is lost, SessionWorkspace reads that row: `committed` returns the recorded
revision, `ready` retries the same transaction, and `abandoned` returns its
recorded conflict. A connection failure alone never marks an upload abandoned.

### 5.5 Artifact module

The Artifact module streams bounded Run output into a server-generated MinIO
key and stores tenant-scoped metadata. It is separate from SessionWorkspace so
an output download does not silently alter the next Run's files or consume the
workspace quota.

3C exposes tenant-scoped Artifact metadata and download routes. Displaying those
links in Playground and Run Detail remains phase four product-closure work; 3C
proves the routes through authenticated API end-to-end tests.

The routes are `GET /api/v1/artifacts/{artifact_id}` for metadata and
`GET /api/v1/artifacts/{artifact_id}/content` for streamed content. They require
the same explicit Workspace selection as Runs API and return a generic not-found
response for another tenant.

## 6. Data model and object layout

### 6.1 `workspace_revisions`

Required columns: `id`, `workspace_id`, `session_id`, nullable
`parent_revision_id`, `manifest_schema_version`, `manifest_object_key`,
`manifest_sha256`, `total_bytes`, `object_count`, `created_by_run_id`, and
`created_at`. Revisions are immutable. Foreign keys include Workspace scope so a
cross-tenant identifier cannot be attached accidentally.

### 6.2 `object_uploads`

Required columns: `id`, `kind` (`workspace` or `artifact`), `workspace_id`,
`session_id`, `run_id`, nullable `base_revision_id`, nullable
`candidate_revision_id`, nullable `candidate_artifact_id`, `staging_prefix`,
`candidate_index_key`, nullable `final_object_key`, `status`, `cleanup_pending`,
nullable `total_bytes`, nullable `object_count`, nullable
`committed_revision_id`, `expires_at`, and timestamps. The staging prefix and
candidate identifiers are unique and server-generated. Default expiry is 24
hours; successful commit marks the registration rather than relying on bucket
scans.

Statuses are `uploading`, `finalizing`, `ready`, `committed`, `abandoned`, and
`expired`. `candidate_index_key` is generated and stored when the `uploading`
row is first created, before its object exists. Before any content final key is
written, the service copies a small candidate index to that durable key and
verifies it. The index enumerates every intended final key and digest; the row
then moves to `finalizing`. `finalizing` and `ready` rows and their
candidate indexes are GC roots, so a concurrent collector cannot delete an
object being copied or committed. `cleanup_pending` remains true until staging
and any no-longer-needed candidate index have actually been deleted.

### 6.3 Run cleanup intent

The existing `runs` table gains nullable `workspace_cleanup_target`
(`paused_limit`, `queued`, or `failed_conflict`) and
`workspace_cleanup_sandbox_id`. A transaction that records a rollback
ToolResultBlock also records where the Run must go after that exact sandbox and
volume are confirmed gone. Worker and Scheduler clear the fields only in the
same transition that reaches the target. This makes a crash between “record the
reason” and “delete the volume” recoverable without inventing state from logs.

### 6.4 `artifacts`

Required columns: `id`, `workspace_id`, `session_id`, `run_id`, `object_key`,
`filename`, `media_type`, `size_bytes`, `sha256`, `truncated`, `expires_at`, and
timestamps. Download always rechecks Workspace membership or API-key scope and
returns a generic not-found result across tenants.

### 6.5 Keys and manifests

Keys are scoped at least to Workspace and Session:

```text
workspaces/{workspace_id}/sessions/{session_id}/staging/{upload_id}/...
workspaces/{workspace_id}/sessions/{session_id}/blobs/sha256/{digest}
workspaces/{workspace_id}/sessions/{session_id}/manifests/{revision_id}.json
workspaces/{workspace_id}/runs/{run_id}/artifacts/{artifact_id}
```

M1 deduplicates unchanged bodies inside one Session only. It does not use a
cross-tenant blob namespace whose existence or timing could leak information.
The manifest is deterministic: normalized UTF-8 paths, bytewise path ordering,
explicit entry type, file mode with privilege bits removed, size, and digest.
Timestamps, uid, gid, xattrs, ACLs, sparse-file layout, and special entries are
not persisted in M1.

Path normalization means UTF-8 NFC with `/` separators and no empty, `.`, or
`..` segment. A Linux filename that is not valid UTF-8 is refused at checkpoint;
two source paths that become equal after normalization are a duplicate-path
error. The lower nine permission bits may be retained, while setuid, setgid,
sticky bits, ownership, and extended attributes are discarded.

## 7. Restore flow

Before the first model call in a fresh SandboxInstance:

1. Worker holds a valid lease and acquires an empty Run-owned data volume.
2. The Worker freezes the fresh container before the first import byte;
   `acquire -> freeze -> import -> verify -> unfreeze` is the only fresh restore
   order.
3. SessionWorkspace reads the Session's current revision. A null revision means
   an empty workspace and needs no object call.
4. It verifies the manifest object's hash and schema version.
5. It streams only ordinary directories and file bodies through the Controller.
   Paths and declared totals are checked before Docker extraction; the fresh
   volume contains no pre-existing symlink to follow.
6. The Controller scans the restored tree while the container is frozen. The
   returned normalized path, entry type, permission bits, size, and digest set
   must equal the manifest.
7. Only after verification does the Worker unfreeze the container and expose
   model or tool schemas.

A transient MinIO or transport failure is `workspace_storage_unavailable` and
leaves the Run `interrupted` for bounded recovery. A verified missing object,
hash mismatch, or unsupported manifest is `workspace_integrity_failed`: the Run
fails, a security audit is written, and an operator must repair or restore data
before a later Run can use that revision. An incomplete import destroys the
suspect sandbox, or isolates it if destruction is unclear. The model never sees
a partially restored tree.

## 8. Write checkpoint flow

After every tool step that may have changed `/workspace/data`:

1. The Worker freezes the sandbox. Background processes cannot change files
   between the scan and export.
2. The Controller scans metadata first. It refuses unsupported entry types and
   returns normalized paths, sizes, and counts without following symlinks.
3. SessionWorkspace compares totals to the quota before uploading bodies.
4. If within quota, it creates an `uploading` `ObjectUpload`, streams changed
   file bodies to its staging prefix, and calculates hashes during the stream.
   It writes the complete candidate manifest under that prefix, copies a compact
   final-key index to the already-registered durable candidate key, verifies it,
   and only then persists `finalizing`. It next uses MinIO
   server-side copy to place verified bodies and the manifest at their final
   keys. After verifying those keys, it marks the upload `ready`. The
   preallocated revision UUID determines the manifest key before the database
   row exists.
5. In one PostgreSQL transaction, the checkpoint committer locks the Session,
   verifies `current_revision == base_revision`, inserts the immutable revision,
   advances `sessions.workspace_revision_id`, updates
   `runs.checkpoint_workspace_revision_id`, records the tool result and event,
   and marks the upload committed.
6. After commit, unchanged blobs may be referenced by the new manifest. The
   Worker then unfreezes, keeps, or destroys the sandbox according to Run state.

After commit, staging and the now-redundant candidate index are deleted and
`cleanup_pending` is cleared. A failed delete leaves that flag set for Scheduler
retry; `committed` never means cleanup was silently forgotten. If the database
result is unknown, the `upload_id` reconciliation rule in §5.4 runs before any
state change. A confirmed conflict makes the registration `abandoned`.
Abandoned cleanup reads the durable candidate index first, removes final objects
only when no other GC root references them, then deletes staging and the index,
and finally marks the upload expired. It never deletes staging before obtaining
the only enumeration of candidate final keys. An uncertain reference keeps the
blob.

If there is no content change, no revision or object upload is created; the tool
result and Run checkpoint continue to reference the same revision.

If the Session revision changed, the database transaction returns
`workspace_conflict`, leaves the current pointer untouched, marks the candidate
abandoned, and transitions the Run to `interrupted` under the existing state
matrix. It never performs a last-writer-wins merge.

Every failure after a model emitted a tool call records a matching failure
ToolResultBlock against the preceding revision. In particular, upload failure
rolls back the dirty sandbox and records `workspace_checkpoint_failed`; conflict
records `workspace_conflict`. The Run may be interrupted or failed, but its
canonical transcript is never left with an unmatched tool call.

## 9. Quotas and over-limit rollback

M1 defaults are:

| Area | Bytes | Filesystem objects | Enforcement |
|---|---:|---:|---|
| `/workspace/data` committed revision | 2 GiB | 100,000 | metadata preflight and checkpoint refusal |
| `/workspace/cache` | 512 MiB | 200,000 inodes | Linux tmpfs `size` and `nr_inodes` |
| `/tmp` | 256 MiB | platform default | existing tmpfs limit |

Directories other than the workspace root count toward the data object limit.
Symlinks and special entries are
refused rather than counted. Platform administrators may change instance
defaults; a Workspace or AgentVersion may only select an equal or smaller
profile.

`/workspace/cache` becomes a tmpfs mount with `rw,nosuid,nodev`, size,
`nr_inodes`, and uid/gid fixed to the sandbox user. It is not `noexec`, because
cache is the intended location for rebuilt dependencies and their executables.
Its pages count toward the container's existing memory limit, so process memory
and cache compete inside the 1 GiB ceiling instead of silently consuming host
memory outside it.

`file.write` calculates the replacement delta and refuses before touching the
file when the new committed totals would exceed the data quota. `shell.exec`
cannot be predicted. If the frozen post-command scan exceeds either data limit:

1. no ObjectUpload or revision is committed;
2. a `workspace_limit_exceeded` event records the measured total, limit, and
   dimension without listing private filenames;
3. a `workspace_limit_exceeded` ToolResultBlock referencing the exact
   ToolCallBlock ID is atomically checkpointed against the preceding revision,
   together with `pause_reason=limit`,
   `workspace_cleanup_target=paused_limit`, and the sandbox ID. Resume thus tells
   the model that the command was rolled back instead of leaving an open tool
   call or replaying it as if nothing happened;
4. the current container and its Run-owned data volume are destroyed;
5. only after confirmed cleanup does the Run enter `paused(limit)`;
6. resume acquires a fresh sandbox and restores the preceding committed
   revision.

This is A1: only the over-limit tool step is rolled back. If destruction is not
confirmed, the Run remains `interrupted` until Scheduler cleanup; it must not
claim a safe pause while the oversized volume may still exist.

The existing state matrix gains one narrowly defined transition:
`interrupted -> paused` after the Scheduler has confirmed cleanup and the Run
already carries `pause_reason=limit`,
`workspace_cleanup_target=paused_limit`, the matching sandbox ID, and the
persisted rollback tool result. No other interrupted Run may use this
transition.

The verification record must retain this exact limitation: a malicious command
can temporarily fill an ordinary named volume before its post-command scan.
Checkpoint quota is not a host-disk hard quota.

## 10. File tools

The M1 tool bindings are `file.list`, `file.read`, `file.write`, and the existing
`shell.exec`.

- All paths are relative to `/workspace/data` and normalized by both the tool
  module and Controller.
- Absolute paths, `..`, NUL, traversal through a symlink, and access outside the
  data mount return `tool_not_authorized` before file content is read.
- `file.list` is bounded and paginated; it does not recursively enumerate an
  unbounded tree in one tool result.
- `file.read` has a per-call byte limit and returns an explicit truncation flag.
- `file.write` has a 16 MiB per-call default, writes a same-directory temporary
  file, fsyncs it, and atomically replaces the target.
- A write does not return success to the model until §8 commits its checkpoint.
- A tool-created unsupported entry, or one left by `shell.exec`, produces
  `workspace_entry_not_supported`. The Controller destroys the current
  container and data volume, reacquires a fresh one, and restores the last
  revision before the Worker records the failure ToolResultBlock and continues.
  If cleanup is unclear, the Run becomes `interrupted`; the illegal entry is
  never left in a sandbox that continues execution.

Tool authorization remains two-step: binding filters the schema before the
model call, and the execution layer rechecks the real name, path, arguments,
Run, lease, and sandbox immediately before use.

## 11. Artifacts and long command output

`execute` becomes a framed output stream rather than returning one completed
byte string. The Controller drains Docker stdout/stderr until process exit or
command timeout. It sends at most the Artifact ceiling to the Worker, keeps the
first 1 MiB as the inline preview, and continues draining and discarding bytes
beyond the ceiling so a noisy child cannot block on a full pipe. The final frame
contains exit code, timeout, total observed bytes, and truncation flags.

When output crosses the preview limit, the Worker registers
`ObjectUpload(kind=artifact)` before uploading any Artifact byte. It uses the
same `uploading -> finalizing -> ready -> committed` recovery chain, with the
preallocated Artifact ID and final key recorded in the row. The Artifact row and
upload `committed` status are created in one transaction. A crash therefore
leaves an enumerable registration, never an invisible final object.

The inline tool-result preview remains capped at 1 MiB. Artifact storage is
capped at 100 MiB per Artifact and 500 MiB per Run. The tool result states
whether the preview or Artifact was truncated and includes an authorized
Artifact identifier, never a raw MinIO URL or key.

If Artifact upload fails, the Controller still drains the command output. No
Artifact row is created; the ToolResultBlock carries the preview plus
`artifact_store_failed`, and a RunEvent records the storage failure. Known
command effects are still checkpointed, so the platform does not falsely replay
the command merely because its extended output could not be retained. Artifact
bytes are excluded from SessionWorkspace quota and included in their own Run
totals and retention job.

## 12. Worker, Run state, and sandbox lifecycle

- A tool-enabled fresh sandbox is restored before its first model call.
- Read-only file tools do not create workspace revisions.
- Every successful possibly-writing tool step checkpoints before the next model
  call or slice boundary.
- Once `shell.exec` has run in an instance, a background process may change data
  without another tool call. The Worker therefore performs a final frozen scan
  at every slice, waiting, pause, cancellation, and terminal boundary while that
  instance exists. A no-change scan reuses the current revision.
- Slice freeze is reused for checkpoint consistency; checkpoint finishes before
  the WorkerLease is released.
- `paused`, `waiting_*`, and terminal transitions require that every completed
  write step is committed; an incomplete or explicitly rolled-back step is not
  promoted. The transition also requires confirmed sandbox plus named-volume
  cleanup.
- A same-Run warm instance keeps data and cache. A destroyed instance restores
  data and reports `cache_state=reset`; cache is never reconstructed from the
  transcript.
- A new Run in the same Session gets a new writable data environment populated
  from the Session revision and an empty cache.
- A retry keeps the existing `retry_context_stale` rule: the Session current
  revision must still equal the source checkpoint revision.
- Interrupted recovery adds the same revision equality check. A
  `workspace_conflict` whose observed Session revision differs from the Run
  checkpoint is never automatically requeued; after sandbox cleanup it becomes
  `failed(workspace_conflict)` and releases the Session head.
- A timed-out `shell.exec` may still have a process running inside Docker. Its
  ToolResultBlock is recorded against the preceding revision, then the dirty
  container and data volume are destroyed and restored before execution can
  continue. A timeout never checkpoints files that may still be changing.

Failures map as follows:

| Failure | Behaviour |
|---|---|
| object upload or manifest verification fails | rollback, matched failure tool result, old revision remains current; `interrupted` |
| revision CAS conflict | no overwrite or automatic requeue; cleanup then `failed(workspace_conflict)` |
| transient object-store failure during restore | model not called; `interrupted(workspace_storage_unavailable)` |
| verified missing or hash-invalid content | model not called; `failed(workspace_integrity_failed)` and security audit |
| checkpoint quota exceeded | A1 rollback, confirmed cleanup, `paused(limit)` |
| unsupported workspace entry | rollback current step; explicit tool result |
| data-volume removal unclear | isolate; remain `interrupted` until Scheduler confirms cleanup |
| cache byte/inode ceiling | checkpoint data and ToolResultBlock, clean sandbox, then `paused(limit)` |
| command timeout with live exec | rollback dirty sandbox; explicit timeout ToolResultBlock |

## 13. Scheduler and garbage collection

The Scheduler gains bounded jobs, protected by the existing advisory-lock and
row-claim rules:

1. expire registered staging uploads after 24 hours and delete only their
   server-generated objects using the candidate-index ordering in §8;
2. retry `abandoned` final-object, staging, and candidate-index deletion, and
   retry `committed` rows whose `cleanup_pending` is still true;
3. remove unreferenced Run-owned data volumes by Controller labels after
   confirming no live reservation and no valid lease;
4. enforce Artifact and Session retention policies;
5. report a failed cleanup without marking the object or volume deleted.

The Controller explicitly creates named data volumes with Workspace, Session,
Run, and instance labels. Cache is tmpfs and creates no Docker volume. Volume
enumeration uses labels, not parsing a model-supplied name. A material deletion
is audited with identifiers and counts, not file contents.

Committed content-addressed blobs are not eagerly deleted when one revision is
superseded. M1 retention GC calculates references from retained manifests before
deletion; an uncertain reference keeps the blob.

GC roots are explicit: every `sessions.workspace_revision_id`; every checkpoint
revision of a non-terminal Run or a failed Run still eligible for derived retry;
every revision retained by Workspace policy; and every `finalizing`, `ready`, or
commit-result-unknown ObjectUpload candidate index. The collector takes a
snapshot of these roots under its scan lock and rechecks an object's references
immediately before deletion.

## 14. Security assertions

Automated checks must prove:

1. Cross-Workspace revision, manifest, Artifact, and download requests return a
   generic refusal without resource fields.
2. Import/export accepts no absolute path, `..`, duplicate normalized path,
   symlink, hard link, device, FIFO, socket, setuid bit, or declared total that
   differs from streamed content.
3. Trusted-process code has an architecture test banning `tarfile.extract*`,
   `shutil.unpack_archive`, and equivalent local archive extraction. A malicious
   traversal archive cannot change a sentinel file outside the sandbox volume.
4. Neither a model nor an API body controls MinIO keys, Docker volume names,
   mounts, uid/gid, or quota settings.
5. Worker-originated Controller operations repeat Run, Reservation, and
   live-lease ownership checks. Scheduler cleanup is refused unless no live
   lease exists and the matching Reservation is isolated.
6. API and Worker still cannot reach Docker; the Controller still receives no
   model credential, and the sandbox receives no MinIO credential.
7. `/workspace/cache` is tmpfs with the configured byte and inode ceilings when
   inspected from the real Linux daemon.
8. A failed or over-limit checkpoint cannot advance either Session or Run
   revision pointers.

## 15. Configuration

New or changed defaults:

| Setting | Default |
|---|---:|
| `workspace_max_bytes` | 2 GiB |
| `workspace_max_objects` | 100,000 |
| `workspace_file_list_entries` | 1,000 |
| `workspace_file_read_bytes` | 1 MiB |
| `workspace_file_write_bytes` | 16 MiB |
| `workspace_staging_ttl_seconds` | 86,400 |
| `workspace_transfer_timeout_seconds` | 300 (maximum 1,800) |
| `controller_stream_frame_bytes` | 1 MiB |
| `controller_stream_credit_bytes` | 8 MiB |
| `controller_stream_idle_seconds` | 30 |
| `sandbox_cache_mb` | 512 |
| `sandbox_cache_inodes` | 200,000 |
| `artifact_max_bytes` | 100 MiB |
| `run_artifact_max_bytes` | 500 MiB |
| `artifact_retention_days` | Workspace retention policy |

The old `sandbox_disk_mb` setting is removed rather than kept as a number that
does nothing. Upgrade notes map its default intent to `workspace_max_bytes` and
state that the latter is a checkpoint quota. Unknown legacy non-default values
stop migration with an actionable message instead of being silently redefined.

## 16. Verification strategy

### 16.1 Unit tests

- deterministic manifest ordering and hashing;
- framed-stream sequence, credit, total, digest, progress, cancellation,
  half-frame, idle, and operation-deadline rules;
- replacement size delta and object counting;
- exactly-at-limit success and one-byte/one-object refusal;
- path normalization, duplicate paths, and every unsupported entry type;
- a racing process that repeatedly swaps a checked directory for a symlink
  cannot escape the held data-root descriptor;
- over-limit rollback persists a tool result against the preceding revision;
- only an interrupted Run with the recorded pending limit pause may use the new
  `interrupted -> paused` transition;
- unchanged checkpoint creates no revision;
- object keys are server-generated and tenant-scoped;
- Artifact and staging limits.
- file-tool capability probing fails closed only for AgentVersions that bind
  `file.*`; a shell-only Agent remains runnable.

### 16.2 PostgreSQL and MinIO integration tests

- upload bodies and manifest before the revision transaction;
- crash after registration, during body upload, after manifest upload, and
  before/after database commit;
- finalizing and ready candidates remain GC roots during a concurrent scan;
- an unknown database answer is reconciled by upload ID rather than abandoned;
- two Runs commit from one base revision and exactly one advances the Session;
- committed upload, abandoned upload, expiry, and retryable cleanup;
- committed staging deletion failure leaves `cleanup_pending` and is retried;
- retry accepts the same revision and rejects a changed revision;
- tenant-isolated Artifact download and retention.

### 16.3 Real Docker tests

- empty, 1 MiB, and 1,000-file workspaces restore byte-identically;
- a killed Worker or deleted container restores the last committed revision;
- cache `size+1` and `inode+1` writes receive `ENOSPC`;
- cache `ENOSPC` checkpoints its tool result and pauses after confirmed cleanup;
- freeze/thaw keeps cache; destroy/recreate resets it;
- archive/path/special-entry attacks fail without a host write;
- over-limit `shell.exec` advances no revision, cleans the current volume, and
  resumes from the preceding revision;
- command output beyond 1 MiB produces a complete bounded Artifact; output past
  the Artifact cap is drained and explicitly marked truncated;
- Artifact upload failure leaves a matched preview ToolResultBlock and an
  enumerable cleanup registration;
- data and cache leave no volume after their lifecycle ends.

### 16.4 End-to-end and performance tests

- create an Agent with file and command tools, write in Run 1, read in Run 2;
- retrieve Artifact metadata and bytes through tenant-authorized API routes;
- API snapshots and events expose Workspace conflict and A1 limit pause data
  needed by phase four UI without requiring that UI in 3C;
- 1 MiB one-file incremental commit P95 is at most 1 second;
- 1,000 files totalling 100 MiB commit P95 is at most 15 seconds;
- same-Session next-Run first-tool availability retains the existing 3-second
  target for a 1 MiB Workspace and cached runtime image.

These 3C measurements are early regression gates for the new storage path.
Phase four still runs and records the complete product-design §24.1 reference
benchmark; 3C does not move the rest of that release gate forward.

The benchmark streams data and records peak memory; no implementation may load
the full 2 GiB quota into a trusted process merely because small tests pass.

## 17. Documentation and compatibility changes

Implementation must update, not silently contradict:

- product design §§16.4, 21.3, 23, 24.1, 25, and 27.1;
- M1 technical design §§6.4, 7.3, 10.3-10.4, 11.2-11.5, 18, and 19;
- phase-3B sandbox design §§7, 10.2, 11-15, and its next seam;
- phase-3B plan Task 3 and completion checklist;
- M1 roadmap phase three;
- `docs/development.md` and the phase-3B verification record.

The phase-3B record keeps its historical evidence and links to this design. It
does not retroactively claim the missing named-volume hard limit passed. The 3C
record separately proves checkpoint quota and cache tmpfs limits.

## 18. Exit criteria

- [ ] File tools pass both authorization checks and cannot escape data.
- [ ] A committed revision survives Worker, container, and process restarts.
- [ ] The Session pointer, Run checkpoint, tool result, event, and upload status
      change atomically.
- [ ] Concurrent base revisions cannot overwrite each other.
- [ ] Exactly-at-limit commits and one-unit-over refusals are tested.
- [ ] A1 rolls back only the over-limit step and resumes from the prior revision.
- [ ] Cache bytes and inodes are physically bounded by tmpfs in Linux CI.
- [ ] Long output produces a tenant-authorized Artifact with explicit truncation.
- [ ] Scheduler enumerates and cleans staging rows and labelled volumes without
      a whole-bucket or arbitrary-host-path scan.
- [ ] Existing phase 1 through 3B checks pass unchanged or are deliberately
      updated where this design replaces a documented placeholder.
- [ ] Documentation calls data quota a checkpoint quota and does not claim a
      physical host-disk ceiling.

Once this spec is approved, the next artifact is a task-by-task 3C implementation
plan. Production code remains gated on that plan and test-first execution.
