# M1 SessionWorkspace, File Tools, and Artifacts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver phase 3C: persistent Session files with immutable revisions, safe `file.*` tools, portable checkpoint quotas, bounded cache, recoverable object uploads, and tenant-authorized Artifacts for long command output.

**Architecture:** A new `session_workspace` deep module owns manifests, MinIO objects, revision comparison, and checkpoint commit; callers see only `restore()` and `checkpoint()`. The Sandbox Controller still owns Docker and streams file bytes over its private Unix socket without receiving MinIO credentials. The Worker freezes the sandbox before scan/export, commits the workspace pointer and matching tool result together, and destroys dirty state before reporting rollback or pause.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, PostgreSQL 17, Alembic, MinIO Python SDK, asyncio Unix sockets, docker-py, a small Linux C helper using `openat2`, pytest, Docker Compose, GitHub Actions.

---

## 1. Fixed scope and working rules

- Work in `.worktrees/m1-run-execution` on branch `m1-sandbox`.
- The authority is `docs/superpowers/specs/2026-08-11-m1-session-workspace-design.md`, then product design v2.4 and M1 technical design v1.1.
- `/workspace/data` has a **commit/checkpoint quota**, not a portable physical host-disk hard limit. Never describe it as a hard disk quota.
- `/workspace/cache` is disposable tmpfs with kernel-enforced byte and inode limits. It is never uploaded or restored.
- The Controller has Docker credentials but no MinIO credentials. The Worker has MinIO credentials but no Docker socket.
- No trusted process extracts an archive onto its own filesystem. Import/export is streamed to or from the Docker volume through the Controller.
- Every model ToolCall gets exactly one persisted ToolResult with the same call ID, including quota, timeout, conflict, unsupported entry, and storage failures.
- Observe a focused failing test before production code. Run the focused test after the implementation and commit only when it passes.
- Database integration tests target only `tiny_hermes_test`.
- Use `uv run --no-sync`; do not run `ruff format`.
- After every task, append a short plain-language explanation and the verified command to `.superpowers/m1-implementation-learning-notes.md`. This file is Git-ignored and must never be staged.

## 2. File map

```text
packages/backend/src/tiny_hermes/session_workspace/
├─ domain/models.py              # manifest entries, outcomes, upload states, quotas
├─ domain/manifest.py            # normalization, deterministic hashing, quota checks
├─ ports/store.py                # revision/upload/atomic-commit repository contract
├─ ports/objects.py              # bounded object-store contract
├─ ports/sandbox.py              # restore/scan/export stream contract
├─ application/service.py        # the public restore/checkpoint deep-module API
├─ application/committer.py      # cross-table commit and upload reconciliation
├─ application/cleanup.py        # upload and blob GC decisions
├─ infrastructure/tables.py      # workspace_revisions and object_uploads
├─ infrastructure/sql_store.py   # PostgreSQL implementation
└─ infrastructure/minio_store.py # only MinIO client construction point

packages/backend/src/tiny_hermes/artifacts/
├─ domain/models.py
├─ ports/store.py
├─ application/service.py
├─ infrastructure/tables.py
├─ infrastructure/sql_store.py
└─ presentation/routes.py

packages/backend/src/tiny_hermes/sandbox/
├─ transport/frames.py           # length-framed stream codec and bounded credit
├─ transport/client.py           # control and streaming client
├─ transport/server.py           # streaming negotiation and dispatch
├─ transport/adapter.py          # wire values -> domain values
├─ application/controller.py     # ownership checks for every new action
├─ infrastructure/docker_engine.py # volume/import/export/exec streaming
└─ domain/container_policy.py    # named data volume plus bounded cache tmpfs

packages/backend/src/tiny_hermes/tools/
├─ domain/files.py               # file requests, responses, limits, path rules
├─ domain/registry.py            # schemas and second authorization check
└─ application/execute.py        # dispatch and deferred write success

deploy/sandbox/file_helper.c     # fixed openat2-based Linux file helper
deploy/sandbox/Dockerfile        # compile/install helper and capability probe
migrations/versions/20260811_0007_session_workspace.py
deploy/compose/compose.yaml      # MinIO credentials and 3C limits
.github/workflows/ci.yml         # MinIO plus real-Docker 3C tests
scripts/workspace_drill.py       # restart, rollback, conflict, Artifact drill
docs/superpowers/verification/2026-08-11-m1-session-workspace.md
```

---

### Task 1: Deterministic manifests, paths, and quota arithmetic

**Files:**
- Create: `packages/backend/src/tiny_hermes/session_workspace/__init__.py`
- Create: `packages/backend/src/tiny_hermes/session_workspace/domain/__init__.py`
- Create: `packages/backend/src/tiny_hermes/session_workspace/domain/models.py`
- Create: `packages/backend/src/tiny_hermes/session_workspace/domain/manifest.py`
- Create: `packages/backend/tests/unit/session_workspace/__init__.py`
- Create: `packages/backend/tests/unit/session_workspace/test_manifest.py`

- [x] **Step 1: Write the failing manifest tests**

Define tests for these concrete cases:

```python
def test_manifest_is_nfc_sorted_and_hash_is_deterministic() -> None:
    first = WorkspaceEntry.file("z.txt", b"z", mode=0o664)
    second = WorkspaceEntry.file("e\u0301.txt", "é".encode(), mode=0o4755)
    manifest = build_manifest((first, second), schema_version=1)
    assert [entry.path for entry in manifest.entries] == ["é.txt", "z.txt"]
    assert manifest.entries[0].mode == 0o755
    assert manifest.sha256 == hashlib.sha256(manifest.canonical_bytes()).hexdigest()

@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../x", "a//x", "a/./x", "a\\x", "a\x00x"])
def test_invalid_manifest_path_is_refused(path: str) -> None:
    with pytest.raises(InvalidWorkspacePath):
        normalize_workspace_path(path)

def test_one_byte_and_one_object_over_limit_are_distinct() -> None:
    assert measure(entries_with(bytes=100, objects=2), WorkspaceQuota(100, 2)).allowed
    assert measure(entries_with(bytes=101, objects=2), WorkspaceQuota(100, 2)).dimension == "bytes"
    assert measure(entries_with(bytes=100, objects=3), WorkspaceQuota(100, 2)).dimension == "objects"
```

Also test invalid UTF-8, NFC duplicates, symlink/hard-link/device/FIFO/socket rejection, directory counting, lower-nine mode preservation with setuid/setgid/sticky removal, and replacement-delta arithmetic for `file.write`.

- [x] **Step 2: Prove the tests fail for the missing module**

Run:

```powershell
uv run --no-sync pytest packages/backend/tests/unit/session_workspace/test_manifest.py -q
```

Expected: collection fails with `ModuleNotFoundError: tiny_hermes.session_workspace`.

- [x] **Step 3: Implement the domain types and pure functions**

Use these names consistently for the rest of the plan:

```python
class EntryType(StrEnum):
    DIRECTORY = "directory"
    FILE = "file"

@dataclass(frozen=True)
class WorkspaceEntry:
    path: str
    entry_type: EntryType
    mode: int
    size: int
    sha256: str | None

@dataclass(frozen=True)
class WorkspaceManifest:
    schema_version: int
    entries: tuple[WorkspaceEntry, ...]
    total_bytes: int
    object_count: int

@dataclass(frozen=True)
class WorkspaceQuota:
    max_bytes: int
    max_objects: int

class CheckpointStatus(StrEnum):
    UNCHANGED = "unchanged"
    COMMITTED = "committed"
    LIMIT_EXCEEDED = "limit_exceeded"
    CONFLICT = "conflict"
    STORAGE_FAILED = "storage_failed"
```

`canonical_bytes()` must use UTF-8 JSON with sorted object keys and compact separators; entry ordering is bytewise order of normalized UTF-8 path bytes. `normalize_workspace_path()` accepts `/` only as separator, forbids empty, `.`, `..`, NUL and absolute paths, and returns NFC.

- [x] **Step 4: Verify and commit**

Run:

```powershell
uv run --no-sync pytest packages/backend/tests/unit/session_workspace/test_manifest.py -q
uv run --no-sync ruff check packages/backend/src/tiny_hermes/session_workspace packages/backend/tests/unit/session_workspace
uv run --no-sync pyright packages/backend/src/tiny_hermes/session_workspace packages/backend/tests/unit/session_workspace
```

Expected: all tests pass, ruff and pyright exit 0.

Commit:

```powershell
git add packages/backend/src/tiny_hermes/session_workspace packages/backend/tests/unit/session_workspace
git commit -m "feat: define session workspace manifests"
```

### Task 2: MinIO configuration and a bounded object adapter

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `packages/backend/src/tiny_hermes/shared/config.py`
- Create: `packages/backend/src/tiny_hermes/session_workspace/ports/__init__.py`
- Create: `packages/backend/src/tiny_hermes/session_workspace/ports/objects.py`
- Create: `packages/backend/src/tiny_hermes/session_workspace/infrastructure/__init__.py`
- Create: `packages/backend/src/tiny_hermes/session_workspace/infrastructure/minio_store.py`
- Create: `packages/backend/tests/unit/session_workspace/test_object_keys.py`
- Create: `packages/backend/tests/integration/session_workspace/__init__.py`
- Create: `packages/backend/tests/integration/session_workspace/test_minio_store.py`
- Modify: `.env.example`
- Modify: `deploy/compose/compose.yaml`

- [x] **Step 1: Write failing settings and key-generation tests**

Assert that Settings requires `s3_access_key` and `s3_secret_key`, validates every 3C ceiling from design §15, refuses a workspace transfer timeout above 1,800 seconds, and no adapter method accepts an arbitrary object key from an API/model request. The key builder must produce exactly:

```text
workspaces/{workspace_id}/sessions/{session_id}/staging/{upload_id}/{name}
workspaces/{workspace_id}/sessions/{session_id}/blobs/sha256/{digest}
workspaces/{workspace_id}/sessions/{session_id}/manifests/{revision_id}.json
workspaces/{workspace_id}/runs/{run_id}/artifacts/{artifact_id}
```

- [x] **Step 2: Prove the unit tests fail**

Run `uv run --no-sync pytest packages/backend/tests/unit/session_workspace/test_object_keys.py packages/backend/tests/unit/shared/test_config.py -q`.

Expected: missing settings and object adapter imports fail.

- [x] **Step 3: Add the SDK, settings, and adapter**

Add `minio>=7.2.18` as a runtime dependency and regenerate `uv.lock` with `uv lock`. `MinioObjectStore` constructs the MinIO client only in `minio_store.py` and wraps blocking SDK calls in `asyncio.to_thread`. Its port exposes bounded `put_stream`, `get_stream`, `stat`, `server_copy`, and `delete_many`; callers pass typed server-generated object references, not strings from requests.

Compose and `.env.example` add:

```text
S3_ACCESS_KEY=tiny-hermes-local
S3_SECRET_KEY=tiny-hermes-local-password
```

The integration test creates a unique prefix, uploads bytes in multiple chunks, verifies SHA-256 and size, copies server-side, streams them back, and deletes its own objects in `finally`.

- [x] **Step 4: Run MinIO integration and commit**

> Executed 2026-08-13 without a reachable Docker daemon on the development
> machine: the six MinIO integration tests skip with a named reason (verified),
> and unit + ruff + pyright pass. The round-trip runs for real in CI (Task 15)
> and on any machine with the compose MinIO up.

Run:

```powershell
docker compose -f deploy/compose/compose.yaml up -d minio
uv run --no-sync pytest packages/backend/tests/integration/session_workspace/test_minio_store.py -q
uv run --no-sync ruff check packages/backend/src/tiny_hermes/shared/config.py packages/backend/src/tiny_hermes/session_workspace
uv run --no-sync pyright packages/backend/src/tiny_hermes/shared/config.py packages/backend/src/tiny_hermes/session_workspace
```

Expected: test passes without reading the full object into memory.

Commit `pyproject.toml`, `uv.lock`, config, compose, env example, adapter, and tests as `feat: add bounded object storage adapter`.

### Task 3: Workspace, upload, Artifact, and cleanup-intent tables

**Files:**
- Create: `migrations/versions/20260811_0007_session_workspace.py`
- Create: `packages/backend/src/tiny_hermes/session_workspace/infrastructure/tables.py`
- Create: `packages/backend/src/tiny_hermes/artifacts/__init__.py`
- Create: `packages/backend/src/tiny_hermes/artifacts/domain/models.py`
- Create: `packages/backend/src/tiny_hermes/artifacts/infrastructure/tables.py`
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/tables.py`
- Modify: `packages/backend/src/tiny_hermes/shared/database.py`
- Modify: `packages/backend/tests/integration/conftest.py`
- Create: `packages/backend/tests/integration/session_workspace/test_migration.py`

- [x] **Step 1: Write schema tests before the migration**

Inspect PostgreSQL, not only SQLAlchemy metadata. Assert every design §6.1–6.4 column, immutable revision metadata, enum/check constraints, composite tenant foreign keys, unique `staging_prefix` and `candidate_index_key`, and these Run fields:

```text
workspace_cleanup_target nullable
workspace_cleanup_sandbox_id nullable UUID
```

Assert `object_uploads.status` accepts only `uploading`, `finalizing`, `ready`, `committed`, `abandoned`, `expired`; `kind` accepts only `workspace`, `artifact`; and cleanup target accepts only `paused_limit`, `queued`, `failed_conflict`.

- [x] **Step 2: Run the new test and observe the missing revision**

```powershell
$env:DATABASE_URL='postgresql+asyncpg://tiny_hermes:local-only@localhost:5432/tiny_hermes_test'
$env:TEST_DATABASE_URL=$env:DATABASE_URL
uv run --no-sync pytest packages/backend/tests/integration/session_workspace/test_migration.py -q
```

Expected: fails because the tables/columns do not exist.

- [x] **Step 3: Implement tables and reversible migration**

Use UUID primary keys, UTC timestamps, and explicit indexes for `workspace_id`, `session_id`, `run_id`, `status`, `expires_at`, and `cleanup_pending`. Register both table modules on `Base.metadata` before the integration fixture derives its `TRUNCATE` statement. Downgrade removes only revision `0007` objects and returns to `20260811_0006_sandbox_event`.

- [x] **Step 4: Verify upgrade, downgrade, upgrade and commit**

Run:

```powershell
uv run --no-sync alembic upgrade head
uv run --no-sync pytest packages/backend/tests/integration/session_workspace/test_migration.py packages/backend/tests/integration/test_initial_migration.py -q
uv run --no-sync alembic downgrade 20260811_0006
uv run --no-sync alembic upgrade head
```

Expected: all commands exit 0.

Commit as `feat: add workspace and artifact storage schema`.

> Verified 2026-08-13 against real PostgreSQL 17.6: at revision 0006 all eight
> schema tests fail with `NoSuchTableError` (the right reason); at head all
> pass; downgrade to 0006 and re-upgrade both exit 0. The Task 2 MinIO tests
> also ran for real here (6 passed), and the full integration suite (275) and
> unit suite (456) are green. The code itself landed in the preceding `wip:`
> commit made to hand work over from the development machine; this commit
> records the verification.

### Task 4: ObjectUpload lifecycle and final-object protection

**Files:**
- Create: `packages/backend/src/tiny_hermes/session_workspace/ports/store.py`
- Create: `packages/backend/src/tiny_hermes/session_workspace/infrastructure/sql_store.py`
- Create: `packages/backend/src/tiny_hermes/session_workspace/application/cleanup.py`
- Create: `packages/backend/tests/integration/session_workspace/test_upload_lifecycle.py`
- Create: `packages/backend/tests/integration/session_workspace/test_upload_gc_race.py`

- [x] **Step 1: Write lifecycle and race tests**

Cover this exact graph:

```text
uploading -> finalizing -> ready -> committed
uploading/finalizing/ready -> abandoned -> expired
uploading/finalizing/ready -> expired (TTL cleanup)
```

The row and its durable `candidate_index_key` are created before any final object. Tests must pause GC while a candidate is `finalizing` and `ready` and prove those final objects survive; a committed row with failed staging deletion keeps `cleanup_pending=true`; an unknown database result is re-read by `upload_id`; a connection failure alone never changes the row to `abandoned`.

- [x] **Step 2: Run tests to establish failure**

Run `uv run --no-sync pytest packages/backend/tests/integration/session_workspace/test_upload_lifecycle.py packages/backend/tests/integration/session_workspace/test_upload_gc_race.py -q`.

Expected: fails because `WorkspaceStore` and cleanup planner are missing.

- [x] **Step 3: Implement the state changes with database guards**

Expose typed commands rather than a general status setter:

```python
register_upload(command: RegisterUpload) -> ObjectUpload
mark_finalizing(upload_id: UUID, index_sha256: str) -> ObjectUpload
mark_ready(upload_id: UUID, totals: UploadTotals) -> ObjectUpload
mark_committed(upload_id: UUID, revision_id: UUID | None, artifact_id: UUID | None) -> None
abandon(upload_id: UUID, reason: str) -> None
claim_cleanup(now: datetime, limit: int) -> tuple[ObjectUpload, ...]
finish_cleanup(upload_id: UUID) -> None
```

Every update includes its expected prior state in SQL. Candidate-index cleanup order is: read and verify index, recheck GC roots, delete unreferenced final keys, delete staging, delete index, then set `expired` and `cleanup_pending=false`. An uncertain reference keeps the object and row claim retryable.

- [x] **Step 4: Verify and commit**

Run the two focused tests plus `uv run --no-sync pyright packages/backend/src/tiny_hermes/session_workspace` and expect all pass. Commit as `feat: make object uploads recoverable`.

> Implementation notes, 2026-08-13: migration 0007 gained
> `candidate_index_sha256` (GC refuses index bytes the commit never verified)
> and `abandon_reason` (history for postmortems) — revised in place because the
> revision has never left this branch. The `ObjectStore` port gained
> `list_prefix` and a distinct `ObjectMissing`, both needed by cleanup.

### Task 5: Framed streaming subprotocol for the controller socket

**Files:**
- Create: `packages/backend/src/tiny_hermes/sandbox/transport/frames.py`
- Create: `packages/backend/tests/unit/sandbox/test_stream_frames.py`

The current protocol is one 64 KiB JSON line per call and stays for small control
calls. Workspace bytes and long command output need design §5.3's framed stream:
typed frames, 1 MiB frame cap, 8 MiB receive credit, a server-checked declared
total, a running SHA-256, and a 30-second idle rule. All of it is a pure codec
plus a receiver state machine — no sockets — for the same reason
`container_policy.py` is pure: the rules must be testable on Windows.

- [x] **Step 1: Write the failing codec and receiver tests**

```python
def test_frames_round_trip_and_reject_oversize() -> None:
    frame = Frame(FrameType.DATA, sequence=1, payload=b"x" * 100)
    encoded = encode_frame(frame)
    decoded, rest = decode_frame(encoded + b"tail")
    assert decoded == frame and rest == b"tail"
    with pytest.raises(FrameTooLarge):
        encode_frame(Frame(FrameType.DATA, sequence=1, payload=b"x" * (1_048_576 + 1)))

def test_receiver_refuses_a_sequence_gap_and_data_after_end() -> None:
    receiver = StreamReceiver(declared_limit=10_000)
    assert receiver.accept(Frame(FrameType.START, 0, b'{"total_limit": 10000}')).ok
    assert receiver.accept(Frame(FrameType.DATA, 1, b"aa")).ok
    assert receiver.accept(Frame(FrameType.DATA, 3, b"bb")).reason == StreamRefusal.SEQUENCE_GAP

def test_receiver_enforces_declared_total_digest_and_credit() -> None: ...
def test_end_frame_must_carry_matching_totals_and_digest() -> None: ...
def test_idle_and_operation_deadlines_are_decisions_not_sleeps() -> None:
    # The receiver is told the clock; it never reads one. `next_deadline()`
    # returns when the caller must have seen a DATA or PROGRESS frame.
    ...
```

Cover: half-frame (decode returns `None`, keeps the buffer), unacknowledged
bytes beyond 8 MiB refused, `credit(n)` restoring the window, cancellation
mid-stream, an END digest mismatch, and a declared total above the operation's
server-side limit refused at START.

- [x] **Step 2: Prove the codec is missing**

Run `uv run --no-sync pytest packages/backend/tests/unit/sandbox/test_stream_frames.py -q`.
Expected: `ModuleNotFoundError` for `frames`.

- [x] **Step 3: Implement the codec and receiver**

Wire format per frame: 4-byte big-endian payload length, 1 type byte, 8-byte
big-endian sequence, payload. Public names:

```python
class FrameType(StrEnum):
    START = "start"; DATA = "data"; PROGRESS = "progress"
    END = "end"; CANCEL = "cancel"; ERROR = "error"

@dataclass(frozen=True)
class Frame:
    type: FrameType
    sequence: int
    payload: bytes

MAX_FRAME_PAYLOAD = 1_048_576
RECEIVE_CREDIT = 8_388_608
IDLE_SECONDS = 30

class StreamRefusal(StrEnum):
    SEQUENCE_GAP = "sequence_gap"; OVER_DECLARED_TOTAL = "over_declared_total"
    DIGEST_MISMATCH = "digest_mismatch"; DATA_AFTER_END = "data_after_end"
    CREDIT_EXCEEDED = "credit_exceeded"; TOTAL_ABOVE_LIMIT = "total_above_limit"

def encode_frame(frame: Frame) -> bytes: ...
def decode_frame(buffer: bytes) -> tuple[Frame, bytes] | None: ...

class StreamReceiver:
    def __init__(self, *, declared_limit: int) -> None: ...
    def accept(self, frame: Frame) -> ReceiverDecision: ...
    def credit(self, consumed_bytes: int) -> None: ...
```

START's payload is JSON: `{"total_limit": int}`; END's is
`{"total_bytes": int, "frame_count": int, "sha256": str}` and the receiver
verifies all three. The receiver hashes DATA payloads as they arrive.

- [x] **Step 4: Verify and commit**

Run the focused test file plus ruff and pyright over `sandbox/transport`.
Commit as `feat: frame the controller's streaming subprotocol`.

> Implementation notes, 2026-08-13: the refusal enum gained `TOTAL_MISMATCH`
> (an END whose totals disagree with what arrived is a different lie than data
> overrunning the declaration) and `MALFORMED`. `CREDIT_EXCEEDED` is the one
> non-terminal refusal — it means "stop reading", and the same frame is
> re-presented after `credit()`.

### Task 6: Cache becomes bounded tmpfs; the data volume gets labels

**Files:**
- Modify: `packages/backend/src/tiny_hermes/sandbox/domain/container_policy.py`
- Modify: `packages/backend/src/tiny_hermes/shared/config.py`
- Modify: `packages/backend/tests/unit/sandbox/test_container_policy.py`

Design §1 and §9: `/workspace/cache` moves from an unenforceable named volume to
a kernel-bounded tmpfs, and the 2 GiB `disk_mb` number — which Docker never
enforced — leaves `ResourceProfile` so no reader can mistake it for a physical
limit again. `workspace_max_bytes` (Task 2's setting) is its successor and is a
checkpoint quota.

- [ ] **Step 1: Write the failing policy tests**

```python
def test_cache_is_tmpfs_with_bytes_inodes_and_sandbox_ownership() -> None:
    config = default_config()
    cache = config.tmpfs["/workspace/cache"]
    assert cache == "rw,nosuid,nodev,size=512m,nr_inodes=200000,uid=10001,gid=10001,mode=0700"
    # Not noexec: cache is where rebuilt dependencies and their executables live.
    assert "noexec" not in cache
    assert [m.target for m in config.mounts] == ["/workspace/data"]

def test_data_volume_carries_platform_labels() -> None:
    config = default_config()
    assert config.volume_labels["tiny-hermes.run"] == str(RUN_ID)
    assert config.volume_labels["tiny-hermes.workspace"] == str(WORKSPACE_ID)
    assert config.volume_labels["tiny-hermes.session"] == str(SESSION_ID)

def test_disk_mb_no_longer_exists() -> None:
    assert not hasattr(DEFAULT_PROFILE, "disk_mb")
```

Update `test_the_disk_ceiling_is_declared_but_not_enforced` — the honest gap it
recorded is now closed by the checkpoint quota, so the test is replaced, not
deleted silently: its replacement asserts the profile carries `cache_mb` and
`cache_inodes` and that `exceeds()` compares them.

- [ ] **Step 2: Run and watch the old shape fail**

`uv run --no-sync pytest packages/backend/tests/unit/sandbox/test_container_policy.py -q`
fails on the new assertions.

- [ ] **Step 3: Reshape the profile and config**

`ResourceProfile`: drop `disk_mb`, add `cache_mb: int` and `cache_inodes: int`
(defaults 512 and 200,000 in `DEFAULT_PROFILE`); `fields()` and `exceeds()`
follow. `container_config()` gains `workspace_id` and `session_id` keyword
arguments; `ContainerConfig` gains `volume_labels: dict[str, str]` (workspace,
session, run, instance) used by Task 8's explicit volume creation, and its cache
mount becomes the tmpfs entry above. `as_docker_kwargs()`'s literal test updates
with it. Settings gain `sandbox_cache_mb` (default 512, ge=64, le=4096) and
`sandbox_cache_inodes` (default 200_000, ge=10_000, le=1_000_000); the
controller CLI threads them into the profile ceiling.

- [ ] **Step 4: Verify and commit**

Focused policy tests, then the whole unit suite (`uv run --no-sync pytest
packages/backend/tests/unit -q`) because the worker and controller tests build
configs. Commit as `feat: bound the cache with tmpfs, not a promise`.

### Task 7: The openat2 file helper and the image that carries it

**Files:**
- Create: `deploy/sandbox/file_helper.c`
- Modify: `deploy/sandbox/Dockerfile`
- Create: `packages/backend/tests/integration/sandbox/test_file_helper.py`

Design §5.2: `file.*` never trusts a checked path string. A fixed helper binary
inside the image opens `/workspace/data` once and resolves every descendant with
`openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)`. The
Controller execs only this helper for file tools; no user string is
interpolated into its argv beyond the relative path and byte limits it
validates.

- [ ] **Step 1: Write the failing integration tests (real Docker)**

In `test_file_helper.py`, start one runtime container (reuse the sandbox test
fixtures), then exec the helper directly:

```python
async def test_helper_reads_writes_and_lists_beneath_data_root(sandbox) -> None:
    await helper(sandbox, "write", "notes/a.txt", stdin=b"hello")
    assert (await helper(sandbox, "read", "notes/a.txt")).stdout == b"hello"
    listing = json.loads((await helper(sandbox, "list", "notes")).stdout)
    assert listing["entries"][0]["path"] == "a.txt"

async def test_helper_refuses_symlink_traversal_even_when_racing(sandbox) -> None:
    # A background loop swaps `dir` between a real directory and a symlink to
    # /etc while the helper reads `dir/target` — every success must have come
    # from the real directory, never from /etc.
    ...

async def test_helper_probe_reports_openat2_support(sandbox) -> None:
    result = await helper(sandbox, "probe")
    assert result.exit_code == 0

async def test_write_is_atomic_same_directory_tmp_plus_rename(sandbox) -> None: ...
```

- [ ] **Step 2: Run against the current image and fail**

`uv run --no-sync pytest packages/backend/tests/integration/sandbox/test_file_helper.py -q`
fails: the binary does not exist in the image.

- [ ] **Step 3: Write the helper and the multi-stage build**

`file_helper.c` subcommands, all relative to a data-root fd opened once from
argv `--root /workspace/data`:

- `probe` — attempts `openat2` with the three RESOLVE flags on the root; exit 0
  or a nonzero exit the platform maps to `file_safety_unavailable`.
- `list <rel> <offset> <limit>` — JSON entries (path, type, size, mode) in
  bytewise order; refuses non-directories; never follows symlinks.
- `read <rel> <max_bytes>` — raw bytes to stdout; exit 3 with `truncated` on
  stderr JSON when the file is larger.
- `write <rel> <max_bytes>` — stdin to `O_TMPFILE`-style same-directory
  temporary (`.tmp-<pid>` via `openat2`), fsync file and directory, `renameat`
  over the target.

Every path walk uses `openat2` per segment-resolved relative path; `..`,
absolute paths, and NUL are refused before any syscall. The Dockerfile becomes
two stages: a `gcc` build stage compiling `file_helper.c` with
`-static -O2 -Wall -Werror`, and the existing runtime stage copying it to
`/usr/local/bin/tiny-hermes-file-helper`. Nothing else in the image changes.

- [ ] **Step 4: Rebuild, verify, and commit**

```powershell
docker build -t tiny-hermes-sandbox:test -f deploy/sandbox/Dockerfile deploy/sandbox
uv run --no-sync pytest packages/backend/tests/integration/sandbox/test_file_helper.py -q
```

Commit as `feat: resolve sandbox file paths in the kernel, not in strings`.

### Task 8: The engine learns volumes, archives, and streamed exec

**Files:**
- Modify: `packages/backend/src/tiny_hermes/sandbox/infrastructure/docker_engine.py`
- Modify: `packages/backend/src/tiny_hermes/sandbox/domain/command.py`
- Create: `packages/backend/tests/integration/sandbox/test_engine_workspace.py`

The engine stays rule-free. It gains the mechanical operations Task 9's
Controller actions authorize: explicit volume lifecycle, tar-stream import and
export against a paused container, a metadata scan that never extracts to the
host filesystem, and an exec that drains output past a cap instead of buffering
it.

- [ ] **Step 1: Write the failing engine tests (real Docker)**

```python
async def test_volume_lifecycle_with_labels() -> None:
    name = await engine.create_volume(f"tiny-hermes-data-{run_id}", labels)
    assert name in [v.name for v in await engine.volumes_labelled("tiny-hermes.run")]
    await engine.remove_volume(name)

async def test_import_then_scan_round_trips_paths_sizes_and_digests() -> None:
    # import a two-file tar into a paused container's /workspace/data, then
    # scan: entries come back normalized, hashed, and no symlink is followed.
    ...

async def test_scan_reports_special_entries_rather_than_skipping_them() -> None:
    # a symlink planted by exec shows up as entry_type "symlink" so the caller
    # can refuse the checkpoint; the scan itself does not fail.
    ...

async def test_exec_stream_drains_beyond_the_cap_and_reports_totals() -> None:
    # `yes | head -c 300M` with a 1 MiB preview and 100 MiB artifact ceiling:
    # the child exits (nothing blocks on a full pipe), observed_bytes ~300M,
    # artifact bytes exactly the ceiling, preview exactly 1 MiB, both flagged.
    ...

async def test_cache_tmpfs_returns_enospc_at_size_and_inode_ceilings() -> None:
    # design §16.3, against the real Linux daemon: `dd` one byte past
    # `sandbox_cache_mb` fails with ENOSPC, and `touch` past
    # `sandbox_cache_inodes` fails with ENOSPC, while /workspace/data accepts
    # both. Skipped where the daemon is not Linux-native.
    ...

async def test_cache_survives_freeze_thaw_and_dies_with_the_instance() -> None:
    ...
```

- [ ] **Step 2: Run and fail on missing methods**

`uv run --no-sync pytest packages/backend/tests/integration/sandbox/test_engine_workspace.py -q`.

- [ ] **Step 3: Implement against docker-py**

New engine surface (all through `_call`/`asyncio.to_thread`):

```python
async def create_volume(self, name: str, labels: dict[str, str]) -> str
async def remove_volume(self, name: str) -> None
async def volumes_labelled(self, label: str) -> list[VolumeInfo]
async def import_tree(self, container_id: str, target: str, tar_stream: AsyncIterator[bytes]) -> None      # put_archive
def export_tree(self, container_id: str, source: str) -> AsyncIterator[bytes]                              # get_archive
async def scan_tree(self, container_id: str, source: str) -> tuple[ScannedEntry, ...]
async def execute_streamed(self, container_id: str, command: SandboxCommand, sink: OutputSink) -> StreamedResult
```

`scan_tree` reads the `get_archive` tar with `tarfile.open(mode="r|")` and
iterates members, hashing file contents from the stream — it never calls
`extract*` and never touches the host filesystem, which Task 15's architecture
test asserts. `ScannedEntry` carries raw path, entry type (including symlink,
device, fifo, socket — the caller refuses, the scanner reports), mode, size,
digest. `execute_streamed` uses `exec_start(stream=True, demux=False)`, feeds an
`OutputSink` protocol (`preview_limit`, `artifact_limit`), keeps draining after
both caps, and returns exit code, timeout flag, observed totals, truncation
flags. `SandboxCommand.output_limit` keeps meaning the preview cap.

- [ ] **Step 4: Verify and commit**

Focused engine tests plus `uv run --no-sync pytest packages/backend/tests/integration/sandbox -q`
(the 3B suite must not regress). Commit as
`feat: teach the engine volumes, archives, and streamed exec`.

### Task 9: Controller workspace actions, ownership checks, and negotiation

**Files:**
- Modify: `packages/backend/src/tiny_hermes/sandbox/application/controller.py`
- Modify: `packages/backend/src/tiny_hermes/sandbox/transport/server.py`
- Modify: `packages/backend/src/tiny_hermes/sandbox/transport/client.py`
- Modify: `packages/backend/src/tiny_hermes/sandbox/transport/adapter.py`
- Create: `packages/backend/tests/unit/sandbox/test_controller_workspace.py`
- Create: `packages/backend/tests/integration/sandbox/test_transport_streaming.py`

- [ ] **Step 1: Write the failing authorization tests first**

Every new action re-checks what `execute` already checks, plus the freeze
requirement design §7/§8 impose:

```python
async def test_workspace_actions_require_a_live_matching_lease() -> None: ...
async def test_import_scan_export_require_a_frozen_instance() -> None:
    # NOT_FROZEN refusal: scanning a running container would checkpoint files
    # a background process is still changing.
    ...
async def test_import_into_a_dirty_instance_is_refused_without_reset() -> None:
    # an interrupted import marks the instance dirty; only destroy is allowed.
    ...
async def test_volume_remove_uses_scheduler_authority_rules() -> None:
    # no live lease, reservation isolated, and only server-recorded IDs.
    ...
```

- [ ] **Step 2: Run them against the six-action Controller and fail**

`uv run --no-sync pytest packages/backend/tests/unit/sandbox/test_controller_workspace.py -q`.

- [ ] **Step 3: Implement actions and the framed negotiation**

`ACTIONS` gains `workspace_import`, `workspace_scan`, `workspace_export`,
`volume_remove`, `execute_stream`. `RefusalReason` gains `NOT_FROZEN` and
`INSTANCE_DIRTY`. Controller methods:

```python
async def workspace_import(self, *, run_id, lease_id, sandbox_id, declared_total) -> ImportHandle
async def workspace_scan(self, *, run_id, lease_id, sandbox_id) -> tuple[ScannedEntry, ...]
async def workspace_export(self, *, run_id, lease_id, sandbox_id, paths) -> ExportHandle
async def volume_remove(self, *, run_id, sandbox_id) -> None   # Scheduler authority, like cleanup
```

`acquire` now also creates the labelled data volume explicitly
(`engine.create_volume`) before the container, and `_remove` deletes container
then volume, recording both confirmations. `sandbox_instances` keeps holding
only server-side IDs.

Transport: a control line whose action is streaming
(`{"action": "workspace_import", ...}`) is answered with
`{"result": {"stream": "ready"}}`, after which the same connection switches to
Task 5 frames until END/ERROR/CANCEL, then returns to line mode for the final
JSON result. The server enforces `MAX_FRAME_BYTES` per frame, per-operation
declared totals from settings (`workspace_transfer_timeout_seconds`,
`controller_stream_*`), and the idle rule using `StreamReceiver.next_deadline`.
The client grows matching `send_stream`/`receive_stream` helpers. The
integration test round-trips a multi-frame import and export over a real Unix
socket (skipped on Windows exactly like the existing transport tests).

- [ ] **Step 4: Verify and commit**

Unit controller tests on Windows; transport streaming plus 3B transport suite
in WSL/CI. Commit as `feat: stream workspaces through the controller's socket`.

### Task 10: SessionWorkspace restore and checkpoint, and the committer

**Files:**
- Create: `packages/backend/src/tiny_hermes/session_workspace/ports/sandbox.py`
- Create: `packages/backend/src/tiny_hermes/session_workspace/application/service.py`
- Create: `packages/backend/src/tiny_hermes/session_workspace/application/committer.py`
- Create: `packages/backend/tests/unit/session_workspace/test_service.py`
- Create: `packages/backend/tests/integration/session_workspace/test_checkpoint_commit.py`

This is the deep module. Callers see exactly two operations (design §5.1); the
manifest arithmetic (Task 1), object adapter (Task 2), upload lifecycle
(Task 4), and sandbox streams (Task 9) are hidden behind it.

- [ ] **Step 1: Write the failing service tests**

Unit tests drive the flow with fake ports:

```python
async def test_restore_verifies_manifest_before_any_body_and_refuses_mismatch() -> None: ...
async def test_restore_of_null_revision_makes_no_object_call() -> None: ...
async def test_checkpoint_unchanged_creates_no_upload_and_no_revision() -> None: ...
async def test_checkpoint_over_quota_refuses_before_uploading_bodies() -> None: ...
async def test_checkpoint_uploads_bodies_then_manifest_then_commits() -> None:
    # order asserted by a recording fake: staging puts, candidate index copy,
    # finalizing, server-side copies, ready, then the database transaction.
    ...
async def test_unsupported_entry_type_refuses_the_checkpoint() -> None: ...
```

Integration tests in `test_checkpoint_commit.py` use real PostgreSQL + MinIO:

```python
async def test_two_commits_from_one_base_revision_exactly_one_advances() -> None: ...
async def test_commit_transaction_is_atomic_across_all_five_tables() -> None:
    # revision row, session pointer, run checkpoint marker, tool turn,
    # run event, upload status: kill the transaction between any two and
    # nothing moved.
async def test_lost_database_answer_reconciles_by_upload_id() -> None: ...
async def test_conflict_marks_candidate_abandoned_and_keeps_the_pointer() -> None: ...
```

- [ ] **Step 2: Run and fail on the missing service**

Both files fail with missing imports.

- [ ] **Step 3: Implement service, port, committer**

```python
@dataclass(frozen=True)
class WorkspaceRestore:
    workspace_id: UUID; session_id: UUID; run_id: UUID
    lease_id: UUID; sandbox_id: UUID

@dataclass(frozen=True)
class WorkspaceCheckpoint:
    workspace_id: UUID; session_id: UUID; run_id: UUID
    lease_id: UUID; sandbox_id: UUID
    base_revision_id: UUID | None
    quota: WorkspaceQuota
    #: What the committer persists with the revision, in one transaction.
    turns: tuple[CanonicalMessage, ...]
    slice_command: RecordSliceCommand

class SessionWorkspaceService:
    async def restore(self, command: WorkspaceRestore) -> RestoreResult: ...
    async def checkpoint(self, command: WorkspaceCheckpoint) -> CheckpointResult: ...
```

`SandboxWorkspacePort` (ports/sandbox.py) wraps the controller client:
`scan()`, `import_stream()`, `export_stream()` — typed frames in, domain values
out, no Docker types. `restore` reads the Session's current revision itself
(locking it), verifies the manifest object's hash, streams bodies, then
re-scans and compares the restored tree to the manifest before returning.

The committer owns the one PostgreSQL transaction of design §8 step 5: open a
session, `SELECT ... FOR UPDATE` the Session row, compare
`workspace_revision_id == base_revision_id` (else `CheckpointStatus.CONFLICT`),
insert the revision, advance the pointer, set
`runs.checkpoint_workspace_revision_id`, call
`SqlRunStore(session).record_slice(...)` with the tool turns so the transcript
and the pointer move together, mark the upload committed. The preallocated
`upload_id` reconciliation from Task 4 runs before any retry. Post-commit
staging cleanup failure only leaves `cleanup_pending` set.

- [ ] **Step 4: Verify and commit**

Unit on Windows; integration with Postgres + MinIO up. Commit as
`feat: give a session a workspace it can trust`.

### Task 11: file.list, file.read, file.write

**Files:**
- Create: `packages/backend/src/tiny_hermes/tools/domain/files.py`
- Modify: `packages/backend/src/tiny_hermes/tools/domain/registry.py`
- Modify: `packages/backend/src/tiny_hermes/tools/application/execute.py`
- Create: `packages/backend/tests/unit/tools/test_file_tools.py`

- [ ] **Step 1: Write the failing tool tests**

```python
@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../../x", "a\x00b"])
def test_file_paths_outside_data_are_refused_before_execution(path: str) -> None:
    with pytest.raises(ToolRefused) as refused:
        authorize(bound=("file.read",), call=_call("file.read", {"path": path}))
    assert refused.value.reason is RefusalReason.NOT_AUTHORIZED

def test_file_read_carries_its_byte_limit_and_truncation_is_explicit() -> None: ...
def test_file_write_refuses_bodies_over_sixteen_mib() -> None: ...
def test_file_list_is_paginated_not_recursive() -> None: ...
def test_unbound_file_tool_is_refused_even_when_implemented() -> None: ...
def test_schemas_advertise_only_bound_tools() -> None:
    assert [s["function"]["name"] for s in schemas_for(("file.read", "shell.exec"))] == ["file.read", "shell.exec"]
```

- [ ] **Step 2: Run and fail**

`uv run --no-sync pytest packages/backend/tests/unit/tools/test_file_tools.py -q`.

- [ ] **Step 3: Implement**

`IMPLEMENTED_TOOLS` becomes `("shell.exec", "file.list", "file.read",
"file.write")` with one schema each (relative `path`; `content` for write;
`offset`/`limit` for list). `files.py` owns request validation: normalize with
Task 1's `normalize_workspace_path` (one rule, used at both edges), then build
the helper invocation:

```python
@dataclass(frozen=True)
class FileToolCommand:
    helper_argv: list[str]        # ["/usr/local/bin/tiny-hermes-file-helper", "read", ...]
    stdin: bytes | None           # the write body
    changes_workspace: bool       # write=True, read/list=False
```

`AuthorizedCall` gains `changes_workspace: bool` (shell.exec is always True).
`run_tool_call` dispatches file commands through the same `controller.execute`
seam with the helper argv — never through bash — and translates helper exit
codes: 0 to output, the truncation exit to an explicit flag in the result, any
refusal to `tool_not_authorized`. A `file.write` result is **built but not yet
persisted as success**; the Worker hands it to the committer, which is what
makes "a write does not return success until §8 commits" structural rather than
polite.

- [ ] **Step 4: Verify and commit**

Focused tests plus the existing `tests/unit/tools` suite. Commit as
`feat: let an agent touch files, three verbs at a time`.

### Task 12: The Worker restores, checkpoints, rolls back, and pauses honestly

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/application/worker.py`
- Modify: `packages/backend/src/tiny_hermes/runs/domain/models.py`
- Modify: `packages/backend/src/tiny_hermes/runs/domain/state_machine.py`
- Modify: `packages/backend/src/tiny_hermes/runs/infrastructure/sql_store.py`
- Create: `packages/backend/tests/unit/runs/test_workspace_transitions.py`
- Create: `packages/backend/tests/integration/runs/test_worker_workspace.py`

The checkpoint unit is one tool round: every tool call of the round executes,
then one frozen scan and one commit cover the round's effects, before the next
model call. That satisfies design §8's "after every tool step that may have
changed data" with the transcript's own turn structure — an over-quota round
rolls back as one step, and each of its write-capable calls receives a
`workspace_limit_exceeded` result naming its own call ID.

- [ ] **Step 1: Write the failing state-machine tests**

```python
def test_only_a_recorded_limit_pause_may_leave_interrupted_for_paused() -> None:
    view = RunStateView(state=RunState.INTERRUPTED)
    decision = machine.decide(view, RunSignal.LIMIT_CLEANUP_CONFIRMED,
                              pause_reason=PauseReason.LIMIT)
    assert decision.state is RunState.PAUSED
    with pytest.raises(InvalidStateMetadata):
        machine.decide(view, RunSignal.LIMIT_CLEANUP_CONFIRMED)  # no reason

def test_no_other_interrupted_signal_reaches_paused() -> None: ...
```

And the store-level guard in `test_worker_workspace.py`: applying
`LIMIT_CLEANUP_CONFIRMED` to a Run whose `workspace_cleanup_target` is not
`paused_limit`, or whose recorded sandbox ID differs, raises; the Scheduler is
the only caller.

- [ ] **Step 2: Write the failing worker-flow integration tests**

With a fake controller and real PostgreSQL + MinIO:

```python
async def test_fresh_sandbox_restores_before_the_first_model_call() -> None:
    # order asserted on the fake: acquire, freeze, import, verify, thaw, model.
async def test_write_round_checkpoints_before_the_next_model_call() -> None: ...
async def test_over_quota_round_rolls_back_cleans_up_then_pauses_limit() -> None:
    # tool result against the preceding revision, cleanup intent recorded,
    # destroy confirmed, then paused(limit); resume restores the old revision.
async def test_unconfirmed_destroy_stays_interrupted_not_paused() -> None: ...
async def test_conflict_becomes_failed_workspace_conflict_after_cleanup() -> None: ...
async def test_final_frozen_scan_runs_at_slice_boundary_after_shell_exec() -> None: ...
async def test_read_only_round_creates_no_revision() -> None: ...
async def test_shell_timeout_rolls_back_the_dirty_sandbox() -> None: ...
```

- [ ] **Step 3: Implement**

Models: `RunSignal.LIMIT_CLEANUP_CONFIRMED`,
`RunEventType.RUN_LIMIT_CLEANUP_CONFIRMED`, plus
`RunEventType.WORKSPACE_LIMIT_EXCEEDED`, `WORKSPACE_CONFLICT`,
`WORKSPACE_CHECKPOINT_FAILED`, `WORKSPACE_STORAGE_UNAVAILABLE`,
`WORKSPACE_INTEGRITY_FAILED`, `WORKSPACE_ENTRY_NOT_SUPPORTED` (facts, written
explicitly like `SANDBOX_CACHE_RESET`). State machine: the one guarded
transition above. `sql_store.apply_signal` verifies the cleanup-intent columns
for that signal and clears them in the same transition; `_fail`-style paths set
them per design §6.3.

Worker: `_open_sandbox` gains the restore step (freeze → SessionWorkspace
`restore` → thaw) and the `file.*` capability probe when the spec binds a file
tool, failing the Run `file_safety_unavailable` if the helper probe refuses.
`_answer_tools` returns which calls were write-capable; when any were, the
Worker freezes, calls `checkpoint` with the round's turns and the
`RecordSliceCommand` it would otherwise have written itself, and acts on the
result: `committed`/`unchanged` thaw and continue; `limit_exceeded` records
rollback results, destroys, and signals per §9; `conflict` destroys then
signals `INTERRUPTED` with the conflict recorded; `storage_failed` rolls back
the dirty sandbox and interrupts. `_close_sandbox` performs the final frozen
scan through `checkpoint` before freeze-and-keep or destroy at every boundary
while an instance in which `shell.exec` ran still exists. Interrupted recovery
in `recover_interrupted` gains the revision-equality check: observed Session
revision differing from the Run checkpoint refuses automatic requeue and, after
cleanup, becomes `failed`.

- [ ] **Step 4: Verify and commit**

Run the two new files, then the whole runs suite:

```powershell
uv run --no-sync pytest packages/backend/tests/unit/runs packages/backend/tests/integration/runs -q
```

Commit as `feat: checkpoint the workspace with the words that describe it`.

### Task 13: Artifacts for output the message cannot hold

**Files:**
- Create: `packages/backend/src/tiny_hermes/artifacts/ports/store.py`
- Create: `packages/backend/src/tiny_hermes/artifacts/application/service.py`
- Create: `packages/backend/src/tiny_hermes/artifacts/infrastructure/sql_store.py`
- Create: `packages/backend/src/tiny_hermes/artifacts/presentation/routes.py`
- Modify: `packages/backend/src/tiny_hermes/api/app.py` (mount the router)
- Modify: `packages/backend/src/tiny_hermes/tools/application/execute.py`
- Create: `packages/backend/tests/unit/artifacts/test_artifact_service.py`
- Create: `packages/backend/tests/integration/artifacts/test_artifact_routes.py`

- [ ] **Step 1: Write the failing tests**

```python
async def test_output_past_the_preview_registers_an_upload_before_any_byte() -> None: ...
async def test_artifact_upload_failure_keeps_the_preview_and_names_the_failure() -> None:
    # ToolResultBlock carries artifact_store_failed; a RunEvent records it;
    # the command's effects still checkpoint.
async def test_per_artifact_and_per_run_ceilings_are_enforced() -> None: ...
async def test_cross_tenant_artifact_requests_get_a_generic_not_found() -> None: ...
async def test_download_streams_bytes_and_verifies_scope_again() -> None: ...
```

- [ ] **Step 2: Run and fail**

Both new test files fail on missing modules.

- [ ] **Step 3: Implement**

The service implements the `OutputSink` Task 8 defined: preview bytes up to
1 MiB in memory, artifact bytes streamed to the staging prefix of an
`ObjectUpload(kind=artifact)` registered before the first byte, with the
preallocated artifact ID and final key in the row. On command end, finalize
per Task 4's chain and insert the `artifacts` row in the same transaction that
marks the upload committed. The tool result gains
`artifact_id`/`artifact_truncated` fields in its output text — never a MinIO
key. Routes:

```python
GET /api/v1/artifacts/{artifact_id}          # metadata, tenant-scoped
GET /api/v1/artifacts/{artifact_id}/content  # StreamingResponse from MinIO
```

Both use the existing workspace-header dependency the Runs API uses and return
the same generic 404 across tenants. `run_tool_call` threads the sink through
`execute_stream` for `shell.exec`.

- [ ] **Step 4: Verify and commit**

Focused tests plus the API integration suite. Commit as
`feat: keep long output as artifacts, not as luck`.

### Task 14: Scheduler garbage collection

**Files:**
- Modify: `packages/backend/src/tiny_hermes/runs/application/scheduler.py`
- Modify: `packages/backend/src/tiny_hermes/session_workspace/application/cleanup.py`
- Create: `packages/backend/tests/integration/session_workspace/test_scheduler_gc.py`

- [ ] **Step 1: Write the failing GC tests**

```python
async def test_staging_uploads_expire_after_ttl_in_candidate_index_order() -> None: ...
async def test_committed_rows_with_cleanup_pending_are_retried() -> None: ...
async def test_labelled_volumes_without_live_reservation_are_removed() -> None: ...
async def test_gc_roots_protect_finalizing_ready_and_unknown_candidates() -> None: ...
async def test_blob_refcount_is_calculated_from_retained_manifests() -> None: ...
async def test_failed_cleanup_is_reported_not_marked_deleted() -> None: ...
```

- [ ] **Step 2: Run and fail on the missing scans**

- [ ] **Step 3: Implement four bounded scans**

New scan-lock names `UPLOADS`, `VOLUMES`, `BLOBS`, `ARTIFACTS` beside the
existing five, each `try_scan_lock`-guarded and batch-limited. `run_once`
gains them after `_collect_expired_records`. The upload scan claims rows via
Task 4's `claim_cleanup` and executes Task 4's ordering. The volume scan asks
the Controller (`volume_remove`) for labelled volumes whose Run has no live
reservation and no valid lease — enumeration by label through the Controller,
never by name parsing. Blob GC snapshots the design §13 root set under its scan
lock and re-checks references immediately before each delete; an uncertain
reference keeps the blob. The artifact scan enforces `expires_at`. Every
material deletion writes an audit entry with identifiers and counts.

- [ ] **Step 4: Verify and commit**

Focused GC tests plus `tests/integration/runs/test_scheduler.py`. Commit as
`feat: collect what the workspace no longer references`.

### Task 15: Compose, credential boundaries, and CI

**Files:**
- Modify: `deploy/compose/compose.yaml`
- Modify: `.env.example`
- Modify: `.github/workflows/ci.yml`
- Modify: `pyproject.toml` (architecture bans)
- Create: `packages/backend/tests/unit/test_archive_ban.py`

- [ ] **Step 1: Write the failing boundary tests**

```python
def test_no_trusted_process_extracts_archives() -> None:
    # ruff's banned-api covers the names; this test walks the source tree for
    # tarfile.extract / shutil.unpack_archive so a rename cannot dodge it.
def test_minio_client_is_constructed_in_exactly_one_module() -> None: ...
```

- [ ] **Step 2: Split the compose environment**

The `&app-env` anchor splits: a base anchor without credentials, an
`&api-env` adding `S3_ACCESS_KEY`/`S3_SECRET_KEY` (api, worker, scheduler,
migrate), and the controller keeping only the base — the Controller has Docker
and must not gain MinIO, and the model-key passthrough moves out of its
environment for the same reason. The compose-e2e boundary step gains:

```bash
docker compose -f deploy/compose/compose.yaml exec -T controller sh -c '! env | grep -q S3_ACCESS_KEY'
docker compose -f deploy/compose/compose.yaml exec -T worker sh -c 'test ! -e /var/run/docker.sock'
```

- [ ] **Step 3: Wire CI**

`backend-integration` gains a `minio` service container (same image as compose,
health-checked), `S3_*` env, and the new alembic downgrade step
(`downgrade 20260811_0006` + upgrade). The ruff `banned-api` table gains
`tarfile.TarFile.extractall`, `tarfile.TarFile.extract`, and
`shutil.unpack_archive`. The repeat-regression list gains
`test_checkpoint_commit.py`.

- [ ] **Step 4: Verify on a real remote and commit**

Push the branch and watch the full CI run green, including Docker jobs.
Commit as `ci: give the tests an object store and the controller nothing`.

### Task 16: The workspace drill and the performance gates

**Files:**
- Create: `scripts/workspace_drill.py`
- Create: `packages/backend/tests/unit/scripts/test_workspace_drill.py`
- Modify: `.github/workflows/ci.yml` (compose-e2e runs the drill)

- [ ] **Step 1: Write the drill scenarios as functions with unit-tested logic**

Follow `scripts/restart_drill.py`'s structure. Scenarios, each against the
running compose stack through the public API only:

1. write in Run 1, kill the worker container mid-run, verify Run 2 in the same
   Session reads the committed file;
2. an over-quota command pauses the Run with `workspace_limit_exceeded` and
   resume finds the preceding revision;
3. two Sessions cannot see each other's files;
4. a command printing 5 MiB yields a downloadable Artifact whose bytes hash to
   what the command printed;
5. no `tiny-hermes.*`-labelled container **or volume** outlives the drill.

- [ ] **Step 2: Add the performance gates**

In the drill (design §16.4), measured with `time.monotonic` and asserted:
1 MiB single-file commit P95 ≤ 1s over 20 commits; 1,000 files / 100 MiB
commit ≤ 15s; next-Run first-tool availability ≤ 3s with a 1 MiB workspace.
Record peak RSS of the worker container during the 100 MiB commit and assert it
stays under 512 MiB — the streaming claim, measured.

- [ ] **Step 3: Wire into compose-e2e and verify**

After the restart drill step:

```yaml
- name: Workspace drill
  env:
    SANDBOX_IMAGE_DIGEST: ${{ steps.sandbox.outputs.digest }}
  run: uv run --no-sync python scripts/workspace_drill.py
```

The leftover-container check extends to volumes. Run the drill locally against
`docker compose up` first, then in CI.

- [ ] **Step 4: Commit**

Commit as `test: drill the workspace through crashes, quotas, and tenants`.

### Task 17: Documentation and the verification record

**Files:**
- Modify: `docs/development.md`
- Modify: `docs/superpowers/specs/2026-08-11-m1-sandbox-design.md` (§7, §10.2, §11–15, next seams)
- Modify: `docs/superpowers/plans/2026-08-11-m1-sandbox.md` (Task 3 note, checklist)
- Modify: `docs/superpowers/plans/2026-08-10-tiny-hermes-m1-roadmap.md` (phase three)
- Modify: `docs/superpowers/verification/2026-08-11-m1-sandbox.md` (link forward, keep history)
- Create: `docs/superpowers/verification/2026-08-11-m1-session-workspace.md`

- [ ] **Step 1: Update every document design §17 names**

Each edit states what 3C changed rather than silently rewriting history. The
3B record keeps its named `disk_mb` gap and links to the 3C record that closes
it. Everywhere the data quota is described, it is a **checkpoint quota**; no
document may claim a physical host-disk ceiling.

- [ ] **Step 2: Run the full fresh verification**

From a clean environment: `docker compose down -v`, `docker compose up`, unit,
integration (with MinIO), real-Docker, e2e, both drills, and the CI run link.
Follow the 3B record's structure: what was verified by execution, what is
asserted from `inspect`, what Docker-in-Docker made impossible, and the
standing limitation that a malicious command can temporarily fill a named
volume before its post-command scan.

- [ ] **Step 3: Commit**

Commit as `docs: record what the session workspace slice proved`.

---

## 3. Phase 3C completion checklist

- [ ] Every design §18 exit criterion checked against evidence, not intention.
- [ ] File tools pass both authorization checks and cannot escape the data root,
      including under a symlink race.
- [ ] A committed revision survives Worker, container, and process restarts.
- [ ] Session pointer, Run checkpoint, tool turn, event, and upload status move
      in one transaction; concurrent base revisions cannot overwrite each other.
- [ ] Exactly-at-limit commits succeed; one-byte and one-object over refuse.
- [ ] A1 rolls back only the over-limit round; `interrupted -> paused` is
      reachable only with the recorded limit-pause intent.
- [ ] Cache bytes and inodes are physically bounded by tmpfs in Linux CI
      (`ENOSPC` observed, not assumed).
- [ ] Long output produces a tenant-authorized Artifact with explicit
      truncation; artifact upload failure keeps a matched preview result.
- [ ] The Controller has no MinIO credential; the Worker has no Docker socket;
      the sandbox has neither — asserted in the running stack.
- [ ] Scheduler cleans staging rows, candidate indexes, labelled volumes, and
      expired Artifacts without a whole-bucket scan; failures are reported, not
      marked done.
- [ ] Performance gates recorded: 1 MiB commit P95, 100 MiB / 1,000-file
      commit, next-Run availability, streaming memory ceiling.
- [ ] Every phase 1, 2A, 2B, 2C, 3A, and 3B check passes unchanged or its
      deliberate replacement is documented.
- [ ] `docs/superpowers/verification/2026-08-11-m1-session-workspace.md` exists
      and states the checkpoint-quota limitation plainly.

