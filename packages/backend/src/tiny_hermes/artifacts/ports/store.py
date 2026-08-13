"""Artifact persistence, as narrow as the module's two jobs.

Reads are always scoped: there is no ``read(artifact_id)`` without a
workspace, so a cross-tenant identifier cannot even be expressed as a query.
"""

from typing import Protocol
from uuid import UUID

from tiny_hermes.artifacts.domain.models import Artifact
from tiny_hermes.tenancy.domain.models import Role


class ArtifactStore(Protocol):
    async def insert(self, artifact: Artifact) -> None: ...

    async def read_scoped(
        self, artifact_id: UUID, workspace_id: UUID
    ) -> Artifact | None: ...

    async def role_for(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def run_total_bytes(self, run_id: UUID) -> int:
        """What this Run's artifacts already weigh, for the per-Run ceiling."""
        ...
