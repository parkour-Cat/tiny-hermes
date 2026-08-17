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
from uuid import UUID

from tiny_hermes.skills.domain.models import (
    Skill,
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
