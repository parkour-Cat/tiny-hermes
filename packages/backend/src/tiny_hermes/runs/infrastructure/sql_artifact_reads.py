"""Opening a file a Run was passed, and refusing every other one.

The check is a join, not a branch: an Artifact is readable by this Run if the
Run produced it or if a grant row says so, and there is no third clause and no
argument that adds one. That is the same technique `intersect` uses in the
delegation domain — the control is that widening cannot be written, rather than
that it is forbidden.

Its own session, for the reason `SqlMemoryCandidates` gives: this runs in the
middle of answering a tool call, and joining the slice's transaction would hold
it open across a model round. Reading is idempotent, so unlike the memory write
there is no cost to the honesty here at all.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.artifacts.infrastructure.tables import ArtifactGrantRow, ArtifactRow
from tiny_hermes.runs.infrastructure.tables import RunRow
from tiny_hermes.runs.ports.artifacts import ArtifactContent, ArtifactReads
from tiny_hermes.session_workspace.ports.objects import ObjectRef, ObjectStore
from tiny_hermes.tools.domain.registry import MAX_ARTIFACT_READ_BYTES


class SqlArtifactReads(ArtifactReads):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        objects: ObjectStore | None = None,
    ) -> None:
        self._sessions = session_factory
        # Absent in a deployment with no object store. A grant would then name
        # a file nobody can fetch, and saying so is better than an empty read
        # the model would take for an empty file.
        self._objects = objects

    async def read(self, *, run_id: UUID, artifact_id: str) -> ArtifactContent:
        try:
            wanted = UUID(artifact_id)
        except ValueError:
            # Not a refusal about permission: it is not an id at all. Told
            # apart because a model that mistyped one should try again, and one
            # that guessed a real id should not.
            return ArtifactContent(detail=f"{artifact_id} is not a file id")
        async with self._sessions.begin() as session:
            run = await session.get(RunRow, run_id)
            if run is None:  # pragma: no cover - the Worker holds this Run
                return ArtifactContent(detail="this Run is not on record")
            row = await session.scalar(
                select(ArtifactRow)
                .outerjoin(
                    ArtifactGrantRow,
                    (ArtifactGrantRow.artifact_id == ArtifactRow.id)
                    & (ArtifactGrantRow.run_id == run_id),
                )
                .where(
                    ArtifactRow.id == wanted,
                    ArtifactRow.workspace_id == run.workspace_id,
                    (ArtifactRow.run_id == run_id)
                    | (ArtifactGrantRow.id.is_not(None)),
                )
            )
            if row is None:
                # One sentence for "does not exist" and for "not yours",
                # deliberately. Telling them apart would let an Agent discover
                # which ids are real by reading the refusals.
                return ArtifactContent(
                    detail=f"{artifact_id} was not passed to this piece of work"
                )
            found = (row.object_key, row.filename, row.media_type, row.size_bytes)
        key, filename, media_type, size = found
        if size > MAX_ARTIFACT_READ_BYTES:
            # Refused with both numbers rather than truncated, the same rule
            # `skill.load` follows: a model handed a prefix cannot tell that it
            # is holding one.
            return ArtifactContent(
                filename=filename,
                media_type=media_type,
                size_bytes=size,
                detail=(
                    f"{filename} is {size} bytes, over the "
                    f"{MAX_ARTIFACT_READ_BYTES} byte limit for one read"
                ),
            )
        if self._objects is None:
            return ArtifactContent(
                filename=filename,
                media_type=media_type,
                size_bytes=size,
                detail="no object store is configured here",
            )
        chunks: list[bytes] = []
        async for chunk in self._objects.get_stream(ObjectRef(key=key)):
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            text = body.decode()
        except UnicodeDecodeError:
            # A conversation carries text. Saying so beats handing the model
            # mojibake it would then reason about as though it were content.
            return ArtifactContent(
                filename=filename,
                media_type=media_type,
                size_bytes=size,
                detail=f"{filename} is not text and cannot be read into a conversation",
            )
        return ArtifactContent(
            filename=filename,
            media_type=media_type,
            size_bytes=size,
            text=text,
        )
