"""Who may change the catalog, and what a change is allowed to be.

The shape is `AgentCatalog`'s: a `WRITERS` / `READERS` pair, one `_require_role`
that returns whether the actor is acting with platform authority only, and one
`_audit` that says so in the action name. The two-level scope is the secrets
module's: writing a platform skill needs `actor.is_platform_admin`, and a
workspace member reading one gets it read-only.

Product design §15.1 for the catalog, §15.2 for what upload accepts. The one
thing worth reading twice is what upload does *not* accept: a list of files,
never an archive. The browser has a directory picker, so the server never grows
a face that unpacks anything (red line three, on the manual path).
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from tiny_hermes.skills.domain.diff import PackageDiff, diff_packages
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
from tiny_hermes.skills.domain.package import (
    SkillFile,
    SkillPackage,
    SkillPackageRefused,
    parse_package,
)
from tiny_hermes.skills.domain.scan import Finding, blocking, scan
from tiny_hermes.skills.ports.store import (
    DuplicateSkillName,
    SkillStore,
    VersionResult,
)
from tiny_hermes.skills.ports.tarball_source import (
    FetchedTarball,
    TarballSource,
    TarballUnavailable,
)
from tiny_hermes.tenancy.domain.models import Actor, Role

WRITERS = frozenset({Role.WORKSPACE_ADMIN, Role.DEVELOPER})
READERS = frozenset({Role.WORKSPACE_ADMIN, Role.DEVELOPER, Role.VIEWER})

#: One. A Run that proposed forty patches would be a review queue nobody
#: empties, and the second proposal from one Run has never been a different
#: idea — it is the same idea with the model's next thought about it.
MAX_PROPOSALS_PER_RUN = 1


def _now() -> datetime:
    return datetime.now(UTC)


class SkillCatalogError(Exception):
    """Base class for every expected Skill Catalog refusal."""


class ForbiddenSkillAction(SkillCatalogError):
    pass


class UnknownSkill(SkillCatalogError):
    pass


class UnknownSkillVersion(SkillCatalogError):
    pass


class SkillNameTaken(SkillCatalogError):
    pass


class InvalidSkillPackage(SkillCatalogError):
    """The files are not a skill package. Carries the domain's reason verbatim."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SkillNameMismatch(SkillCatalogError):
    """A new version's manifest names a different skill than the one addressed.

    Refused rather than renamed: a skill's name is what an Agent binds and what
    §6 shows the model, so letting an upload move it would change the meaning of
    bindings nobody touched.
    """

    def __init__(self, expected: str, found: str) -> None:
        super().__init__(f"{found!r} uploaded to the skill named {expected!r}")
        self.expected = expected
        self.found = found


class SkillScanRefused(SkillCatalogError):
    """The static scan found something blocking, so this content is not stored.

    Carries the findings, because an author told only "refused" has to guess
    which of forty files holds the key that was pasted into it.
    """

    def __init__(self, findings: Sequence[Finding]) -> None:
        super().__init__(f"{len(findings)} blocking findings")
        self.findings = tuple(findings)


class VersionNotBindable(SkillCatalogError):
    """Asked to make a withdrawn or blocked version the default for new bindings."""


class UnknownProposal(SkillCatalogError):
    pass


class ProposalNotApprovable(SkillCatalogError):
    """Approval refused, with the reason it was refused.

    Two reasons reach here and they are told apart on purpose: a proposal that
    was already decided, and one the scan blocked (§15.3 step 3). The second is
    the one the roadmap names as an exit check, and a reviewer who is only told
    "cannot approve" would go looking for a permission problem.
    """

    def __init__(self, reason: str, findings: Sequence[Finding] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.findings = tuple(findings)


class ProposalLimitReached(SkillCatalogError):
    """One Run has already proposed as much as a Run may propose."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"a Run may open {limit} proposals")
        self.limit = limit


class SkillImportFailed(SkillCatalogError):
    """The archive at that URL could not be fetched or could not be read.

    One refusal for both, carrying the reason as prose, because from the far
    side of the form "GitHub answered 404" and "that tar has a symlink in it"
    are the same kind of problem: the URL that was typed does not work.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SkillCatalog:
    """The catalog's rules, with persistence one operation at a time behind it."""

    def __init__(self, store: SkillStore, tarballs: TarballSource | None = None) -> None:
        self._store = store
        # Optional so the unit tests, which are about who may act and what may
        # be stored, need no way out of the process. An import without a source
        # is refused rather than attempted.
        self._tarballs = tarballs

    async def list_skills(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> Sequence[Skill]:
        await self._require_reader(actor, workspace_id, request_id)
        return await self._store.list_visible(workspace_id)

    async def get_skill(
        self, actor: Actor, workspace_id: UUID, skill_id: UUID, request_id: str
    ) -> Skill:
        await self._require_reader(actor, workspace_id, request_id)
        return await self._visible(workspace_id, skill_id)

    async def list_versions(
        self, actor: Actor, workspace_id: UUID, skill_id: UUID, request_id: str
    ) -> Sequence[SkillVersion]:
        await self._require_reader(actor, workspace_id, request_id)
        skill = await self._visible(workspace_id, skill_id)
        return await self._store.list_versions(skill.id)

    async def read_version(
        self,
        actor: Actor,
        workspace_id: UUID,
        skill_id: UUID,
        version_id: UUID,
        request_id: str,
    ) -> tuple[SkillVersion, tuple[SkillFile, ...]]:
        """The one read that carries bodies. Everything else lists metadata."""
        await self._require_reader(actor, workspace_id, request_id)
        skill = await self._visible(workspace_id, skill_id)
        version = await self._store.get_version(version_id)
        if version is None or version.skill_id != skill.id:
            raise UnknownSkillVersion
        return version, await self._store.read_files(version.id)

    async def create_skill(
        self,
        actor: Actor,
        workspace_id: UUID,
        scope: SkillScope,
        files: Sequence[SkillFile],
        request_id: str,
    ) -> tuple[Skill, SkillVersion]:
        """A new skill and its first version, named by the manifest.

        The name is read out of `SKILL.md` rather than taken as a field so that
        the catalog and the package can never disagree about what this is.
        """
        await self._require_writer(actor, workspace_id, scope, request_id)
        package, findings = _accept(files)
        try:
            skill = await self._store.create_skill(
                scope=scope,
                workspace_id=None if scope is SkillScope.PLATFORM else workspace_id,
                name=package.manifest.name,
                created_by=actor.id,
            )
        except DuplicateSkillName as error:
            raise SkillNameTaken from error
        result = await self._store.add_version(
            skill_id=skill.id,
            package=package,
            findings=findings,
            source=SkillSource.UPLOAD,
            source_url=None,
            source_ref=None,
            created_by=actor.id,
        )
        created = await self._store.set_current_version(skill.id, result.version.id)
        if created is None:
            raise UnknownSkill
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="skill.created",
            resource_id=skill.id,
            request_id=request_id,
            context={"scope": scope.value, "name": skill.name},
        )
        return created, result.version

    async def upload_version(
        self,
        actor: Actor,
        workspace_id: UUID,
        skill_id: UUID,
        files: Sequence[SkillFile],
        request_id: str,
    ) -> VersionResult:
        """A new version of a skill that exists.

        Uploading content this skill already has returns that version with
        `created=False`, and the route answers 200 rather than 201. Re-uploading
        an unchanged directory is not a publication, and a version list that
        grew a duplicate row every time somebody clicked twice would make
        rollback a guessing game.
        """
        skill = await self._writable(actor, workspace_id, skill_id, request_id)
        package, findings = _accept(files)
        if package.manifest.name != skill.name:
            raise SkillNameMismatch(skill.name, package.manifest.name)
        result = await self._store.add_version(
            skill_id=skill.id,
            package=package,
            findings=findings,
            source=SkillSource.UPLOAD,
            source_url=None,
            source_ref=None,
            created_by=actor.id,
        )
        if result.created:
            await self._store.append_audit(
                workspace_id=workspace_id,
                actor_id=actor.id,
                action="skill.version_added",
                resource_id=result.version.id,
                request_id=request_id,
                context={"version": str(result.version.version_number)},
            )
        return result

    async def import_skill(
        self,
        actor: Actor,
        workspace_id: UUID,
        scope: SkillScope,
        url: str,
        request_id: str,
    ) -> tuple[Skill, SkillVersion]:
        """A new skill from a git tarball, named by the manifest inside it."""
        await self._require_writer(actor, workspace_id, scope, request_id)
        fetched = await self._fetch(url)
        package, findings = _accept(fetched.files)
        try:
            skill = await self._store.create_skill(
                scope=scope,
                workspace_id=None if scope is SkillScope.PLATFORM else workspace_id,
                name=package.manifest.name,
                created_by=actor.id,
            )
        except DuplicateSkillName as error:
            raise SkillNameTaken from error
        result = await self._store.add_version(
            skill_id=skill.id,
            package=package,
            findings=findings,
            source=SkillSource.GIT,
            source_url=url,
            source_ref=fetched.ref,
            created_by=actor.id,
        )
        created = await self._store.set_current_version(skill.id, result.version.id)
        if created is None:
            raise UnknownSkill
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="skill.imported",
            resource_id=skill.id,
            request_id=request_id,
            context={"scope": scope.value, "name": skill.name, "url": url},
        )
        return created, result.version

    async def import_version(
        self,
        actor: Actor,
        workspace_id: UUID,
        skill_id: UUID,
        url: str,
        request_id: str,
    ) -> VersionResult:
        """Re-import a skill from source. Unchanged content is not a new version."""
        skill = await self._writable(actor, workspace_id, skill_id, request_id)
        fetched = await self._fetch(url)
        package, findings = _accept(fetched.files)
        if package.manifest.name != skill.name:
            raise SkillNameMismatch(skill.name, package.manifest.name)
        result = await self._store.add_version(
            skill_id=skill.id,
            package=package,
            findings=findings,
            source=SkillSource.GIT,
            source_url=url,
            source_ref=fetched.ref,
            created_by=actor.id,
        )
        if result.created:
            await self._store.append_audit(
                workspace_id=workspace_id,
                actor_id=actor.id,
                action="skill.version_imported",
                resource_id=result.version.id,
                request_id=request_id,
                context={"version": str(result.version.version_number), "url": url},
            )
        return result

    async def _fetch(self, url: str) -> FetchedTarball:
        if self._tarballs is None:
            raise SkillImportFailed("Importing from a URL is not configured here.")
        try:
            return await self._tarballs.fetch(url)
        except TarballUnavailable as error:
            raise SkillImportFailed(str(error)) from error

    async def withdraw_version(
        self,
        actor: Actor,
        workspace_id: UUID,
        skill_id: UUID,
        version_id: UUID,
        request_id: str,
    ) -> SkillVersion:
        """Stop new bindings. Runs already bound to this version keep working."""
        skill = await self._writable(actor, workspace_id, skill_id, request_id)
        version = await self._store.get_version(version_id)
        if version is None or version.skill_id != skill.id:
            raise UnknownSkillVersion
        withdrawn = await self._store.set_version_status(
            version.id, SkillVersionStatus.WITHDRAWN
        )
        if withdrawn is None:
            raise UnknownSkillVersion
        if skill.current_version_id == withdrawn.id:
            # Otherwise the default for new bindings points at something no new
            # binding is allowed to name, and the next create fails with a
            # message about the wrong thing.
            await self._store.set_current_version(skill.id, None)
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="skill.version_withdrawn",
            resource_id=withdrawn.id,
            request_id=request_id,
        )
        return withdrawn

    async def set_current_version(
        self,
        actor: Actor,
        workspace_id: UUID,
        skill_id: UUID,
        version_id: UUID,
        request_id: str,
    ) -> Skill:
        """Move where new bindings start, including backwards.

        No published Agent reads this pointer — an AgentSpec names a version id
        — so rolling it back changes nothing that is already running.
        """
        skill = await self._writable(actor, workspace_id, skill_id, request_id)
        version = await self._store.get_version(version_id)
        if version is None or version.skill_id != skill.id:
            raise UnknownSkillVersion
        if not version.bindable:
            raise VersionNotBindable
        moved = await self._store.set_current_version(skill.id, version.id)
        if moved is None:
            raise UnknownSkill
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="skill.current_version_set",
            resource_id=skill.id,
            request_id=request_id,
            context={"version": str(version.version_number)},
        )
        return moved

    # -- §15.3, self improvement ------------------------------------------
    #
    # The whole of it is here, and the shape says what the roadmap forbids:
    # `propose` writes a proposal and never a version, `approve_proposal` is
    # the only method in this class that turns one into the other, and nothing
    # calls it but a person's request. There is no automatic approval to
    # disable because there is no code that would perform one.

    async def propose(
        self,
        actor: Actor,
        workspace_id: UUID,
        files: Sequence[SkillFile],
        request_id: str,
        skill_id: UUID | None = None,
    ) -> SkillProposal:
        """A person's suggestion, for a skill that exists or one that does not."""
        skill = None
        if skill_id is not None:
            skill = await self._writable(actor, workspace_id, skill_id, request_id)
        else:
            await self._require_writer(actor, workspace_id, SkillScope.WORKSPACE, request_id)
        return await self._propose(
            workspace_id=workspace_id,
            skill=skill,
            base_version_id=None if skill is None else skill.current_version_id,
            files=files,
            origin=ProposalOrigin.HUMAN,
            origin_run_id=None,
            created_by=actor.id,
            request_id=request_id,
        )

    async def propose_from_run(
        self,
        *,
        workspace_id: UUID,
        run_id: UUID,
        created_by: UUID,
        files: Sequence[SkillFile],
        request_id: str,
        skill_id: UUID | None = None,
        base_version_id: UUID | None = None,
    ) -> SkillProposal:
        """An Agent's suggestion, made from inside a Run.

        No role check, and that is not an omission. A Run is executing an Agent
        Version somebody published; the authority it acts with was granted
        then, and there is no interactive actor here to check anything about.
        What keeps this safe is the other end: the result is a `pending` row
        that no Run can approve.

        `created_by` is the person who published the Agent Version. Attributing
        it to nobody was not available — the column is a real user — and
        attributing it to the person who happened to start the Run would point
        a reviewer at someone who never wrote a word of it.
        """
        opened = await self._store.count_proposals_for_run(run_id)
        if opened >= MAX_PROPOSALS_PER_RUN:
            raise ProposalLimitReached(MAX_PROPOSALS_PER_RUN)
        skill = None
        if skill_id is not None:
            skill = await self._store.get_skill(skill_id)
            if skill is None:
                raise UnknownSkill
        return await self._propose(
            workspace_id=workspace_id,
            skill=skill,
            base_version_id=base_version_id,
            files=files,
            origin=ProposalOrigin.AGENT,
            origin_run_id=run_id,
            created_by=created_by,
            request_id=request_id,
        )

    async def _propose(
        self,
        *,
        workspace_id: UUID,
        skill: Skill | None,
        base_version_id: UUID | None,
        files: Sequence[SkillFile],
        origin: ProposalOrigin,
        origin_run_id: UUID | None,
        created_by: UUID,
        request_id: str,
    ) -> SkillProposal:
        """Parse, scan, store — and store even when the scan blocks.

        This is the one place that differs from every other write in this
        class. `_accept` refuses blocking content because a version is served
        into prompts; a proposal is not served anywhere, and the author needs
        to see which of their forty files holds the key they pasted into it.
        §15.3 step 3 takes it from here: it can be read and never approved.
        """
        try:
            package = parse_package(tuple(files))
        except SkillPackageRefused as error:
            raise InvalidSkillPackage(str(error)) from error
        if skill is not None and package.manifest.name != skill.name:
            raise SkillNameMismatch(skill.name, package.manifest.name)
        proposal = await self._store.create_proposal(
            workspace_id=workspace_id,
            skill_id=None if skill is None else skill.id,
            base_version_id=base_version_id,
            package=package,
            findings=scan(package.files),
            origin=origin,
            origin_run_id=origin_run_id,
            created_by=created_by,
        )
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=created_by,
            action="skill.proposal_opened",
            resource_id=proposal.id,
            request_id=request_id,
            context={"origin": origin.value, "name": package.manifest.name},
        )
        return proposal

    async def list_proposals(
        self,
        actor: Actor,
        workspace_id: UUID,
        request_id: str,
        status: ProposalStatus | None = None,
    ) -> Sequence[SkillProposal]:
        await self._require_reader(actor, workspace_id, request_id)
        return await self._store.list_proposals(workspace_id, status)

    async def read_proposal(
        self, actor: Actor, workspace_id: UUID, proposal_id: UUID, request_id: str
    ) -> tuple[SkillProposal, PackageDiff]:
        """The proposal and the difference it would make, read together.

        Together because they are one document to the person deciding, and
        because computing the diff needs the base version's files — a read the
        console would otherwise have to make separately and could get wrong.
        """
        await self._require_reader(actor, workspace_id, request_id)
        proposal = await self._proposal(workspace_id, proposal_id)
        base: tuple[SkillFile, ...] = ()
        if proposal.base_version_id is not None:
            base = await self._store.read_files(proposal.base_version_id)
        return proposal, diff_packages(base, proposal.files)

    async def approve_proposal(
        self, actor: Actor, workspace_id: UUID, proposal_id: UUID, request_id: str
    ) -> tuple[Skill, SkillVersion]:
        """§15.3 steps 4 and 5: a person decides, and a version is published.

        What this deliberately does not do is move `current_version_id` for a
        skill that already had one. Step 6 says the Agent switches to the new
        version explicitly, and moving the default here would make approval and
        switching one action that nobody chose separately. A brand new skill is
        different only because it has nowhere else its default could point.
        """
        proposal = await self._proposal(workspace_id, proposal_id)
        skill: Skill | None = None
        if proposal.skill_id is not None:
            skill = await self._writable(actor, workspace_id, proposal.skill_id, request_id)
        else:
            await self._require_writer(actor, workspace_id, SkillScope.WORKSPACE, request_id)
        if proposal.status is not ProposalStatus.PENDING:
            raise ProposalNotApprovable(f"this proposal is already {proposal.status.value}")
        refusing = blocking(proposal.findings)
        if refusing:
            # The roadmap's exit check, and the reason a blocking proposal is
            # allowed to exist at all: it is readable, and it stops here.
            raise ProposalNotApprovable("the static scan blocked this content", refusing)
        package = parse_package(proposal.files)
        # Decided before the version is written. `decide_proposal` only moves a
        # row that is still `pending`, so two approvals racing produce one
        # version rather than two — and the loser is told the proposal was
        # already decided instead of publishing a duplicate.
        decided = await self._store.decide_proposal(
            proposal.id, ProposalStatus.APPROVED, actor.id, _now()
        )
        if decided is None:
            raise ProposalNotApprovable("this proposal is already decided")
        fresh = skill is None
        if skill is None:
            try:
                skill = await self._store.create_skill(
                    scope=SkillScope.WORKSPACE,
                    workspace_id=workspace_id,
                    name=package.manifest.name,
                    created_by=actor.id,
                )
            except DuplicateSkillName as error:
                # Somebody created the skill between the proposal and this
                # approval. Refused rather than merged into theirs: which of
                # the two contents wins is not a decision this method may make.
                raise SkillNameTaken from error
        result = await self._store.add_version(
            skill_id=skill.id,
            package=package,
            findings=proposal.findings,
            source=SkillSource.PROPOSAL,
            source_url=None,
            source_ref=str(proposal.id),
            created_by=actor.id,
        )
        if fresh:
            moved = await self._store.set_current_version(skill.id, result.version.id)
            if moved is None:
                raise UnknownSkill
            skill = moved
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="skill.proposal_approved",
            resource_id=proposal.id,
            request_id=request_id,
            context={
                "skill_version_id": str(result.version.id),
                "version": str(result.version.version_number),
            },
        )
        return skill, result.version

    async def reject_proposal(
        self, actor: Actor, workspace_id: UUID, proposal_id: UUID, request_id: str
    ) -> SkillProposal:
        """A decision that produces nothing. That is the whole of it."""
        proposal = await self._proposal(workspace_id, proposal_id)
        if proposal.skill_id is not None:
            await self._writable(actor, workspace_id, proposal.skill_id, request_id)
        else:
            await self._require_writer(actor, workspace_id, SkillScope.WORKSPACE, request_id)
        rejected = await self._store.decide_proposal(
            proposal.id, ProposalStatus.REJECTED, actor.id, _now()
        )
        if rejected is None:
            raise ProposalNotApprovable(f"this proposal is already {proposal.status.value}")
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action="skill.proposal_rejected",
            resource_id=proposal.id,
            request_id=request_id,
        )
        return rejected

    async def _proposal(self, workspace_id: UUID, proposal_id: UUID) -> SkillProposal:
        proposal = await self._store.get_proposal(proposal_id)
        if proposal is None or proposal.workspace_id != workspace_id:
            # Another workspace's proposal is not found here, for the reason
            # `_visible` gives about skills.
            raise UnknownProposal
        return proposal

    async def _visible(self, workspace_id: UUID, skill_id: UUID) -> Skill:
        """§9.3's scope check, with §15.1's one exception written out.

        A missing skill and another workspace's skill raise the same refusal on
        purpose: the difference between them is exactly the fact a caller
        outside the workspace is not entitled to learn.
        """
        skill = await self._store.get_skill(skill_id)
        if skill is None:
            raise UnknownSkill
        if skill.scope is SkillScope.PLATFORM:
            return skill
        if skill.workspace_id != workspace_id:
            raise UnknownSkill
        return skill

    async def _writable(
        self, actor: Actor, workspace_id: UUID, skill_id: UUID, request_id: str
    ) -> Skill:
        """Read it first, then check the role its own scope demands.

        Order matters: a workspace developer editing a platform skill has to be
        refused for being the wrong kind of administrator, and that is only
        knowable after the skill has been read.
        """
        skill = await self._visible(workspace_id, skill_id)
        await self._require_writer(actor, workspace_id, skill.scope, request_id)
        return skill

    async def _require_writer(
        self, actor: Actor, workspace_id: UUID, scope: SkillScope, request_id: str
    ) -> None:
        if actor.is_service_account:
            # Skills are authored by people. A key that could rewrite what every
            # Run is told would be a quiet way around §7's approval.
            raise ForbiddenSkillAction
        if scope is SkillScope.PLATFORM:
            if not actor.is_platform_admin:
                raise ForbiddenSkillAction
            await self._store.append_audit(
                workspace_id=None,
                actor_id=actor.id,
                action="skill.platform_write",
                resource_id=workspace_id,
                request_id=request_id,
            )
            return
        await self._require_role(
            actor,
            workspace_id,
            request_id,
            allowed=WRITERS,
            audit_as_platform="skill.write_by_platform_admin",
        )

    async def _require_reader(
        self, actor: Actor, workspace_id: UUID, request_id: str
    ) -> None:
        if actor.is_service_account:
            if actor.role is None or actor.role not in READERS:
                raise ForbiddenSkillAction
            return
        await self._require_role(
            actor,
            workspace_id,
            request_id,
            allowed=READERS,
            audit_as_platform="skill.read_by_platform_admin",
        )

    async def _require_role(
        self,
        actor: Actor,
        workspace_id: UUID,
        request_id: str,
        *,
        allowed: frozenset[Role],
        audit_as_platform: str,
    ) -> None:
        role = await self._store.user_role(workspace_id, actor.id)
        if role is not None:
            if role not in allowed:
                raise ForbiddenSkillAction
            return
        if not actor.is_platform_admin:
            raise ForbiddenSkillAction
        await self._store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            action=audit_as_platform,
            resource_id=workspace_id,
            request_id=request_id,
        )


def _accept(files: Sequence[SkillFile]) -> tuple[SkillPackage, tuple[Finding, ...]]:
    """Parse, scan, and refuse anything blocking before a row exists.

    A proposal carrying the same blocking finding *is* stored, so its author can
    read what the scan said (§15.3). The difference is that a proposal cannot
    run and a version can.
    """
    try:
        package = parse_package(tuple(files))
    except SkillPackageRefused as error:
        raise InvalidSkillPackage(str(error)) from error
    findings = scan(package.files)
    found = blocking(findings)
    if found:
        raise SkillScanRefused(found)
    return package, findings
