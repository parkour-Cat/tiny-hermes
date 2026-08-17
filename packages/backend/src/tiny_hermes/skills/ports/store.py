"""What the catalog needs from persistence, in one place.

Shaped after `agents/ports/store.py`: every method is one atomic operation, and
a `None` result always means the addressed resource is not visible from the
given workspace rather than that something went wrong.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.skills.domain.models import (
    ProposalOrigin,
    ProposalStatus,
    Skill,
    SkillProposal,
    SkillScope,
    SkillSource,
    SkillVersion,
    SkillVersionStatus,
)
from tiny_hermes.skills.domain.package import SkillFile, SkillPackage
from tiny_hermes.skills.domain.scan import Finding
from tiny_hermes.tenancy.domain.models import Role


class DuplicateSkillName(Exception):
    """Two skills in one scope answering to the same name."""


@dataclass(frozen=True)
class VersionResult:
    version: SkillVersion
    #: False when this content was already a version. The caller answers 200
    #: instead of 201 — re-importing the same files is not a new publication.
    created: bool


class SkillStore(Protocol):
    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def create_skill(
        self,
        *,
        scope: SkillScope,
        workspace_id: UUID | None,
        name: str,
        created_by: UUID,
    ) -> Skill: ...

    async def get_skill(self, skill_id: UUID) -> Skill | None: ...

    async def find_skill(
        self, scope: SkillScope, workspace_id: UUID | None, name: str
    ) -> Skill | None: ...

    async def list_visible(self, workspace_id: UUID) -> Sequence[Skill]:
        """This workspace's skills, plus every platform skill."""
        ...

    async def add_version(
        self,
        *,
        skill_id: UUID,
        package: SkillPackage,
        findings: Sequence[Finding],
        source: SkillSource,
        source_url: str | None,
        source_ref: str | None,
        created_by: UUID,
    ) -> VersionResult:
        """Store one immutable version, or return the one this content already is."""
        ...

    async def get_version(self, version_id: UUID) -> SkillVersion | None: ...

    async def list_versions(self, skill_id: UUID) -> Sequence[SkillVersion]: ...

    async def read_files(self, version_id: UUID) -> tuple[SkillFile, ...]:
        """The bodies, read only when somebody actually needs them."""
        ...

    async def set_version_status(
        self, version_id: UUID, status: SkillVersionStatus
    ) -> SkillVersion | None: ...

    async def set_current_version(
        self, skill_id: UUID, version_id: UUID | None
    ) -> Skill | None:
        """Move the default for new bindings. Published Agents do not read it."""
        ...

    async def create_proposal(
        self,
        *,
        workspace_id: UUID | None,
        skill_id: UUID | None,
        base_version_id: UUID | None,
        package: SkillPackage,
        findings: Sequence[Finding],
        origin: ProposalOrigin,
        origin_run_id: UUID | None,
        created_by: UUID,
    ) -> SkillProposal: ...

    async def get_proposal(self, proposal_id: UUID) -> SkillProposal | None: ...

    async def list_proposals(
        self, workspace_id: UUID, status: ProposalStatus | None = None
    ) -> Sequence[SkillProposal]: ...

    async def decide_proposal(
        self,
        proposal_id: UUID,
        status: ProposalStatus,
        decided_by: UUID,
        decided_at: datetime,
    ) -> SkillProposal | None:
        """Record a decision. It never writes a version; §7's approval does."""
        ...

    async def count_proposals_for_run(self, run_id: UUID) -> int: ...

    async def append_audit(
        self,
        *,
        workspace_id: UUID | None,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None: ...
