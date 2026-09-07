from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from tiny_hermes.session_workspace.ports.store import RevisionRecord


@dataclass(frozen=True)
class FileSource:
    workspace_id: UUID
    session_id: UUID
    revision: RevisionRecord | None
    missing_revision: bool = False


class WorkspaceFileStore(Protocol):
    async def run_source(self, workspace_id: UUID, run_id: UUID) -> FileSource | None: ...

    async def session_source(self, workspace_id: UUID, session_id: UUID) -> FileSource | None: ...
