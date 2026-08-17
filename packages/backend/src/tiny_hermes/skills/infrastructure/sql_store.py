from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
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
from tiny_hermes.skills.domain.package import SkillFile, SkillManifest, SkillPackage
from tiny_hermes.skills.domain.scan import Finding, Severity
from tiny_hermes.skills.infrastructure.tables import (
    SkillFileRow,
    SkillProposalRow,
    SkillRow,
    SkillVersionRow,
)
from tiny_hermes.skills.ports.store import DuplicateSkillName, VersionResult
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow


class SqlSkillStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None:
        value = await self._session.scalar(
            select(MembershipRow.role).where(
                MembershipRow.workspace_id == workspace_id,
                MembershipRow.user_id == user_id,
            )
        )
        return Role(value) if value else None

    async def create_skill(
        self,
        *,
        scope: SkillScope,
        workspace_id: UUID | None,
        name: str,
        created_by: UUID,
    ) -> Skill:
        now = datetime.now(UTC)
        row = SkillRow(
            id=uuid4(),
            scope=scope.value,
            workspace_id=workspace_id,
            name=name,
            current_version_id=None,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise DuplicateSkillName from error
        return _skill(row)

    async def get_skill(self, skill_id: UUID) -> Skill | None:
        row = await self._session.get(SkillRow, skill_id)
        return None if row is None else _skill(row)

    async def find_skill(
        self, scope: SkillScope, workspace_id: UUID | None, name: str
    ) -> Skill | None:
        row = await self._session.scalar(
            select(SkillRow).where(
                SkillRow.scope == scope.value,
                SkillRow.workspace_id == workspace_id,
                SkillRow.name == name,
            )
        )
        return None if row is None else _skill(row)

    async def list_visible(self, workspace_id: UUID) -> Sequence[Skill]:
        rows = (
            await self._session.scalars(
                select(SkillRow)
                .where(
                    (SkillRow.scope == SkillScope.PLATFORM.value)
                    | (SkillRow.workspace_id == workspace_id)
                )
                .order_by(SkillRow.scope, SkillRow.name)
            )
        ).all()
        return [_skill(row) for row in rows]

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
        existing = await self._by_content(skill_id, package.content_hash)
        if existing is not None:
            return VersionResult(version=existing, created=False)
        highest = await self._session.scalar(
            select(func.max(SkillVersionRow.version_number)).where(
                SkillVersionRow.skill_id == skill_id
            )
        )
        row = SkillVersionRow(
            id=uuid4(),
            skill_id=skill_id,
            version_number=int(highest or 0) + 1,
            content_hash=package.content_hash,
            manifest=_manifest_json(package.manifest),
            scan_findings=_findings_json(findings),
            source=source.value,
            source_url=source_url,
            source_ref=source_ref,
            status=SkillVersionStatus.ACTIVE.value,
            created_by=created_by,
            created_at=datetime.now(UTC),
        )
        try:
            # A savepoint, so losing the race costs this insert rather than
            # whatever else the caller's transaction had already done.
            async with self._session.begin_nested():
                self._session.add(row)
                # Flushed on its own before the files that point at it. The
                # ORM cannot derive the order here: `skills` and
                # `skill_versions` reference each other, and a cycle in the
                # metadata costs the flush its table sort, so the file rows
                # would otherwise be free to go first and hit the foreign key.
                await self._session.flush()
                for entry in package.files:
                    self._session.add(
                        SkillFileRow(
                            id=uuid4(),
                            skill_version_id=row.id,
                            path=entry.path,
                            content=entry.text,
                        )
                    )
        except IntegrityError:
            # Two callers uploaded the same content at once, or raced on the
            # next version number. The database settled it; re-read rather than
            # report a failure the caller could not act on anyway.
            raced = await self._by_content(skill_id, package.content_hash)
            if raced is None:
                raise
            return VersionResult(version=raced, created=False)
        return VersionResult(version=_version(row), created=True)

    async def get_version(self, version_id: UUID) -> SkillVersion | None:
        row = await self._session.get(SkillVersionRow, version_id)
        return None if row is None else _version(row)

    async def list_versions(self, skill_id: UUID) -> Sequence[SkillVersion]:
        rows = (
            await self._session.scalars(
                select(SkillVersionRow)
                .where(SkillVersionRow.skill_id == skill_id)
                .order_by(SkillVersionRow.version_number)
            )
        ).all()
        return [_version(row) for row in rows]

    async def read_files(self, version_id: UUID) -> tuple[SkillFile, ...]:
        rows = (
            await self._session.scalars(
                select(SkillFileRow)
                .where(SkillFileRow.skill_version_id == version_id)
                .order_by(SkillFileRow.path)
            )
        ).all()
        return tuple(SkillFile(path=row.path, text=row.content) for row in rows)

    async def set_version_status(
        self, version_id: UUID, status: SkillVersionStatus
    ) -> SkillVersion | None:
        row = await self._session.get(SkillVersionRow, version_id)
        if row is None:
            return None
        row.status = status.value
        await self._session.flush()
        return _version(row)

    async def set_current_version(
        self, skill_id: UUID, version_id: UUID | None
    ) -> Skill | None:
        row = await self._session.get(SkillRow, skill_id)
        if row is None:
            return None
        row.current_version_id = version_id
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _skill(row)

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
        row = SkillProposalRow(
            id=uuid4(),
            workspace_id=workspace_id,
            skill_id=skill_id,
            base_version_id=base_version_id,
            files=_files_json(package.files),
            manifest=_manifest_json(package.manifest),
            scan_findings=_findings_json(findings),
            origin=origin.value,
            origin_run_id=origin_run_id,
            status=ProposalStatus.PENDING.value,
            created_by=created_by,
            created_at=datetime.now(UTC),
        )
        self._session.add(row)
        await self._session.flush()
        return _proposal(row)

    async def get_proposal(self, proposal_id: UUID) -> SkillProposal | None:
        row = await self._session.get(SkillProposalRow, proposal_id)
        return None if row is None else _proposal(row)

    async def list_proposals(
        self, workspace_id: UUID, status: ProposalStatus | None = None
    ) -> Sequence[SkillProposal]:
        query = select(SkillProposalRow).where(
            SkillProposalRow.workspace_id == workspace_id
        )
        if status is not None:
            query = query.where(SkillProposalRow.status == status.value)
        rows = (
            await self._session.scalars(
                query.order_by(SkillProposalRow.created_at, SkillProposalRow.id)
            )
        ).all()
        return [_proposal(row) for row in rows]

    async def decide_proposal(
        self,
        proposal_id: UUID,
        status: ProposalStatus,
        decided_by: UUID,
        decided_at: datetime,
    ) -> SkillProposal | None:
        row = await self._session.get(SkillProposalRow, proposal_id)
        if row is None or row.status != ProposalStatus.PENDING.value:
            return None
        row.status = status.value
        row.decided_by = decided_by
        row.decided_at = decided_at
        await self._session.flush()
        return _proposal(row)

    async def count_proposals_for_run(self, run_id: UUID) -> int:
        value = await self._session.scalar(
            select(func.count())
            .select_from(SkillProposalRow)
            .where(SkillProposalRow.origin_run_id == run_id)
        )
        return int(value or 0)

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
        self._session.add(
            AuditEventRow(
                workspace_id=workspace_id,
                actor_type="user",
                actor_id=actor_id,
                action=action,
                resource_type="skill",
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context=context or {},
            )
        )

    async def _by_content(self, skill_id: UUID, content_hash: str) -> SkillVersion | None:
        row = await self._session.scalar(
            select(SkillVersionRow).where(
                SkillVersionRow.skill_id == skill_id,
                SkillVersionRow.content_hash == content_hash,
            )
        )
        return None if row is None else _version(row)


def _manifest_json(manifest: SkillManifest) -> dict[str, Any]:
    return {"name": manifest.name, "description": manifest.description}


def _findings_json(findings: Sequence[Finding]) -> list[Any]:
    return [
        {
            "code": finding.code,
            "severity": finding.severity.value,
            "path": finding.path,
            "detail": finding.detail,
        }
        for finding in findings
    ]


def _files_json(files: Sequence[SkillFile]) -> list[Any]:
    return [{"path": entry.path, "text": entry.text} for entry in files]


def _read_manifest(stored: dict[str, Any]) -> SkillManifest:
    return SkillManifest(
        name=str(stored.get("name", "")), description=str(stored.get("description", ""))
    )


def _read_findings(stored: list[Any]) -> tuple[Finding, ...]:
    return tuple(
        Finding(
            code=str(item.get("code", "")),
            severity=Severity(item.get("severity", Severity.ADVISORY.value)),
            path=str(item.get("path", "")),
            detail=str(item.get("detail", "")),
        )
        for item in stored
    )


def _read_files(stored: list[Any]) -> tuple[SkillFile, ...]:
    return tuple(
        SkillFile(path=str(item.get("path", "")), text=str(item.get("text", "")))
        for item in stored
    )


def _skill(row: SkillRow) -> Skill:
    return Skill(
        id=row.id,
        scope=SkillScope(row.scope),
        workspace_id=row.workspace_id,
        name=row.name,
        current_version_id=row.current_version_id,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _version(row: SkillVersionRow) -> SkillVersion:
    return SkillVersion(
        id=row.id,
        skill_id=row.skill_id,
        version_number=row.version_number,
        content_hash=row.content_hash,
        manifest=_read_manifest(row.manifest),
        findings=_read_findings(row.scan_findings),
        source=SkillSource(row.source),
        source_url=row.source_url,
        source_ref=row.source_ref,
        status=SkillVersionStatus(row.status),
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _proposal(row: SkillProposalRow) -> SkillProposal:
    return SkillProposal(
        id=row.id,
        workspace_id=row.workspace_id,
        skill_id=row.skill_id,
        base_version_id=row.base_version_id,
        files=_read_files(row.files),
        manifest=_read_manifest(row.manifest),
        findings=_read_findings(row.scan_findings),
        origin=ProposalOrigin(row.origin),
        origin_run_id=row.origin_run_id,
        status=ProposalStatus(row.status),
        created_by=row.created_by,
        created_at=row.created_at,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
    )
