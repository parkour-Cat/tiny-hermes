"""The ObjectUpload lifecycle as typed commands, not a status setter.

Design §6.2's graph is the whole contract:

    uploading -> finalizing -> ready -> committed
    uploading/finalizing/ready -> abandoned -> expired
    uploading/finalizing/ready -> expired (TTL cleanup)

A general ``set_status`` would make every caller responsible for the graph;
these commands make the store responsible, and the SQL guard behind each one
makes PostgreSQL the referee when two processes disagree.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.session_workspace.domain.models import UploadKind, UploadStatus


class UnknownUpload(Exception):
    """No registration with that identifier exists."""


class UploadStateConflict(Exception):
    """The row exists but is not where this transition expects it to be.

    Carries the actual status so a caller resolving an unknown outcome can
    read the truth from the refusal instead of issuing another query.
    """

    def __init__(self, upload_id: UUID, actual: UploadStatus) -> None:
        super().__init__(f"upload {upload_id} is {actual.value}")
        self.upload_id = upload_id
        self.actual = actual


@dataclass(frozen=True)
class RegisterUpload:
    """Everything a registration must pin down before any object exists.

    The staging prefix and candidate identifiers are server-generated and
    recorded here first — the row is what lets a collector tell orphaned
    garbage from a commit in flight, so it must precede the garbage.
    """

    upload_id: UUID
    kind: UploadKind
    workspace_id: UUID
    session_id: UUID
    run_id: UUID
    base_revision_id: UUID | None
    candidate_revision_id: UUID | None
    candidate_artifact_id: UUID | None
    staging_prefix: str
    candidate_index_key: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.staging_prefix or not self.candidate_index_key:
            raise ValueError("a registration needs its server-generated keys")
        if self.kind is UploadKind.WORKSPACE:
            if self.candidate_revision_id is None:
                raise ValueError("a workspace upload becomes a revision or nothing")
            if self.candidate_artifact_id is not None:
                raise ValueError("a workspace upload cannot also be an artifact")
        else:
            if self.candidate_artifact_id is None:
                raise ValueError("an artifact upload becomes an artifact or nothing")
            if self.candidate_revision_id is not None or self.base_revision_id is not None:
                raise ValueError("an artifact upload carries no revision identifiers")


@dataclass(frozen=True)
class UploadTotals:
    """What the staged candidate measured out to, recorded at ``ready``."""

    total_bytes: int
    object_count: int
    final_object_key: str | None = None

    def __post_init__(self) -> None:
        if self.total_bytes < 0 or self.object_count < 0:
            raise ValueError("totals cannot be negative")


@dataclass(frozen=True)
class ObjectUpload:
    """One registration as the database currently tells it."""

    upload_id: UUID
    kind: UploadKind
    workspace_id: UUID
    session_id: UUID
    run_id: UUID
    base_revision_id: UUID | None
    candidate_revision_id: UUID | None
    candidate_artifact_id: UUID | None
    staging_prefix: str
    candidate_index_key: str
    candidate_index_sha256: str | None
    final_object_key: str | None
    status: UploadStatus
    cleanup_pending: bool
    total_bytes: int | None
    object_count: int | None
    committed_revision_id: UUID | None
    abandon_reason: str | None
    expires_at: datetime


class WorkspaceStore(Protocol):
    """Upload lifecycle persistence. Every mutation names its prior state."""

    async def read(self, upload_id: UUID) -> ObjectUpload | None:
        """The reconciliation primitive: after an unknown outcome, re-read."""
        ...

    async def register_upload(self, command: RegisterUpload) -> ObjectUpload:
        """Create the ``uploading`` row before any object exists.

        Raises ``IntegrityError`` on a duplicate staging prefix or candidate
        identifier; the caller does not check first.
        """
        ...

    async def mark_finalizing(
        self, upload_id: UUID, *, index_sha256: str
    ) -> ObjectUpload:
        """Record that the durable candidate index is written and verified."""
        ...

    async def mark_ready(self, upload_id: UUID, *, totals: UploadTotals) -> ObjectUpload:
        """Record that every final object is placed and verified."""
        ...

    async def mark_committed(
        self, upload_id: UUID, *, revision_id: UUID | None, artifact_id: UUID | None
    ) -> None:
        """Inside the checkpoint transaction: confirm the registered candidate.

        The guard includes the candidate identifiers, so committing anything
        other than what was registered is a conflict, not an update.
        """
        ...

    async def abandon(self, upload_id: UUID, *, reason: str) -> None: ...

    async def claim_cleanup(
        self, now: datetime, *, limit: int
    ) -> tuple[ObjectUpload, ...]:
        """Rows whose objects may be reclaimed, oldest debt first.

        Never returns an unexpired ``finalizing`` or ``ready`` row — those are
        GC roots while their commit may still be in flight. Mutual exclusion
        between collectors comes from the Scheduler's advisory lock.
        """
        ...

    async def finish_cleanup(self, upload_id: UUID) -> None:
        """Clear the debt after deletion actually happened.

        A committed row keeps its status; every other reclaimed row becomes
        ``expired``. Refuses a row whose ``cleanup_pending`` is already clear,
        because that means the caller's claim was stale.
        """
        ...
