"""The same store without a database, for tests above the persistence line.

It keeps the rules the SQL schema keeps — one name per scope, one version per
content hash — because a test that passes here and fails against Postgres is
worse than no test.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

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
from tiny_hermes.skills.ports.store import DuplicateSkillName, VersionResult
from tiny_hermes.tenancy.domain.models import Role


class MemorySkillStore:
    def __init__(self) -> None:
        self.skills: dict[UUID, Skill] = {}
        self.versions: dict[UUID, SkillVersion] = {}
        self.files: dict[UUID, tuple[SkillFile, ...]] = {}
        self.proposals: dict[UUID, SkillProposal] = {}
        self.memberships: dict[tuple[UUID, UUID], Role] = {}
        self.audit_actions: list[str] = []

    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        return self.memberships.get((workspace_id, user_id))

    async def create_skill(
        self,
        *,
        scope: SkillScope,
        workspace_id: UUID | None,
        name: str,
        created_by: UUID,
    ) -> Skill:
        if await self.find_skill(scope, workspace_id, name) is not None:
            raise DuplicateSkillName
        now = datetime.now(UTC)
        skill = Skill(
            id=uuid4(),
            scope=scope,
            workspace_id=workspace_id,
            name=name,
            current_version_id=None,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        self.skills[skill.id] = skill
        return skill

    async def get_skill(self, skill_id: UUID) -> Skill | None:
        return self.skills.get(skill_id)

    async def find_skill(
        self, scope: SkillScope, workspace_id: UUID | None, name: str
    ) -> Skill | None:
        for skill in self.skills.values():
            if skill.scope is scope and skill.workspace_id == workspace_id:
                if skill.name == name:
                    return skill
        return None

    async def list_visible(self, workspace_id: UUID) -> Sequence[Skill]:
        visible = [
            skill
            for skill in self.skills.values()
            if skill.scope is SkillScope.PLATFORM or skill.workspace_id == workspace_id
        ]
        return sorted(visible, key=lambda item: (item.scope.value, item.name))

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
        for existing in self.versions.values():
            if existing.skill_id == skill_id:
                if existing.content_hash == package.content_hash:
                    return VersionResult(version=existing, created=False)
        numbers = [
            version.version_number
            for version in self.versions.values()
            if version.skill_id == skill_id
        ]
        version = SkillVersion(
            id=uuid4(),
            skill_id=skill_id,
            version_number=max(numbers, default=0) + 1,
            content_hash=package.content_hash,
            manifest=package.manifest,
            findings=tuple(findings),
            source=source,
            source_url=source_url,
            source_ref=source_ref,
            status=SkillVersionStatus.ACTIVE,
            created_by=created_by,
            created_at=datetime.now(UTC),
        )
        self.versions[version.id] = version
        self.files[version.id] = package.files
        return VersionResult(version=version, created=True)

    async def get_version(self, version_id: UUID) -> SkillVersion | None:
        return self.versions.get(version_id)

    async def list_versions(self, skill_id: UUID) -> Sequence[SkillVersion]:
        found = [
            version for version in self.versions.values() if version.skill_id == skill_id
        ]
        return sorted(found, key=lambda item: item.version_number)

    async def read_files(self, version_id: UUID) -> tuple[SkillFile, ...]:
        return self.files.get(version_id, ())

    async def set_version_status(
        self, version_id: UUID, status: SkillVersionStatus
    ) -> SkillVersion | None:
        version = self.versions.get(version_id)
        if version is None:
            return None
        updated = replace(version, status=status)
        self.versions[version_id] = updated
        return updated

    async def set_current_version(
        self, skill_id: UUID, version_id: UUID | None
    ) -> Skill | None:
        skill = self.skills.get(skill_id)
        if skill is None:
            return None
        updated = replace(
            skill, current_version_id=version_id, updated_at=datetime.now(UTC)
        )
        self.skills[skill_id] = updated
        return updated

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
    ) -> SkillProposal:
        proposal = SkillProposal(
            id=uuid4(),
            workspace_id=workspace_id,
            skill_id=skill_id,
            base_version_id=base_version_id,
            files=package.files,
            manifest=package.manifest,
            findings=tuple(findings),
            origin=origin,
            origin_run_id=origin_run_id,
            status=ProposalStatus.PENDING,
            created_by=created_by,
            created_at=datetime.now(UTC),
            decided_by=None,
            decided_at=None,
        )
        self.proposals[proposal.id] = proposal
        return proposal

    async def get_proposal(self, proposal_id: UUID) -> SkillProposal | None:
        return self.proposals.get(proposal_id)

    async def list_proposals(
        self, workspace_id: UUID, status: ProposalStatus | None = None
    ) -> Sequence[SkillProposal]:
        found = [
            proposal
            for proposal in self.proposals.values()
            if proposal.workspace_id == workspace_id
            and (status is None or proposal.status is status)
        ]
        return sorted(found, key=lambda item: (item.created_at, item.id))

    async def decide_proposal(
        self,
        proposal_id: UUID,
        status: ProposalStatus,
        decided_by: UUID,
        decided_at: datetime,
    ) -> SkillProposal | None:
        proposal = self.proposals.get(proposal_id)
        if proposal is None or proposal.status is not ProposalStatus.PENDING:
            return None
        decided = replace(
            proposal, status=status, decided_by=decided_by, decided_at=decided_at
        )
        self.proposals[proposal_id] = decided
        return decided

    async def count_proposals_for_run(self, run_id: UUID) -> int:
        return sum(
            1
            for proposal in self.proposals.values()
            if proposal.origin_run_id == run_id
        )

    async def append_audit(
        self,
        *,
        workspace_id: UUID | None,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None:
        del workspace_id, actor_id, resource_id, request_id, context
        self.audit_actions.append(action)
