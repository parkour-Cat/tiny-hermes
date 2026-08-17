"""What the catalog stores, apart from how it stores it.

Product design §15.1. Three records: a `Skill` is a name and a place in the
two-level catalog, a `SkillVersion` is one immutable content, and a
`SkillProposal` is somebody's suggestion that has not become either.

Two of the four red lines are visible in these shapes rather than in a check:

**Versions are immutable.** `SkillVersion` has no field a later edit writes
except `status`, because §15.1 needs a way to stop serving one. Changing the
content means a new row with a new `version_number`.

**A proposal is not a version.** They are separate types with separate tables,
and the only path from one to the other is a person approving it (§7).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from tiny_hermes.skills.domain.package import SkillFile, SkillManifest
from tiny_hermes.skills.domain.scan import Finding, blocking


class SkillScope(StrEnum):
    """§15.1's two-level catalog.

    `PLATFORM` is the one deliberate exception to §9.3's "every resource query
    applies workspace scope": a platform skill is readable from every workspace
    and writable only by a platform administrator. Being an exception, it gets
    its own tests rather than riding on the shared ones.
    """

    PLATFORM = "platform"
    WORKSPACE = "workspace"


class SkillVersionStatus(StrEnum):
    ACTIVE = "active"
    #: §15.1's "停用". The content stays readable — a Run that already binds
    #: this version must keep working — but nothing new may bind it.
    WITHDRAWN = "withdrawn"


class SkillSource(StrEnum):
    UPLOAD = "upload"
    GIT = "git"
    PROPOSAL = "proposal"


class ProposalOrigin(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Skill:
    id: UUID
    scope: SkillScope
    #: `None` exactly when the scope is `PLATFORM`; the database says so too.
    workspace_id: UUID | None
    name: str
    #: The version a **new** binding starts from, and nothing else. It is not
    #: what published Agents run: an AgentSpec names a version id, so moving
    #: this pointer — including rolling it back — changes no published Agent.
    #: Read §15.1's "回滚" here rather than as "switch production over".
    current_version_id: UUID | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SkillVersion:
    """One immutable content, without the content.

    The files are read separately, because a catalog listing shows twenty of
    these and needs none of their bodies. §6's progressive loading is the same
    idea one level up: a summary is cheap, a body is not.
    """

    id: UUID
    skill_id: UUID
    version_number: int
    content_hash: str
    manifest: SkillManifest
    findings: tuple[Finding, ...]
    source: SkillSource
    #: Where it came from, when that is a place. Both are `None` for an upload.
    source_url: str | None
    #: An immutable reference — a commit sha, an ETag, a proposal id. "Imported
    #: from a branch" is not reproducible two months later; this is.
    source_ref: str | None
    status: SkillVersionStatus
    created_by: UUID
    created_at: datetime

    @property
    def bindable(self) -> bool:
        """Whether a draft may name this version. Publishing checks it again."""
        return self.status is SkillVersionStatus.ACTIVE and not blocking(self.findings)


@dataclass(frozen=True)
class SkillProposal:
    """A suggestion. It carries its files because it is always read whole."""

    id: UUID
    workspace_id: UUID | None
    #: `None` when the proposal is for a skill that does not exist yet; its
    #: name then comes from the manifest, the same as any other new skill.
    skill_id: UUID | None
    base_version_id: UUID | None
    files: tuple[SkillFile, ...]
    manifest: SkillManifest
    findings: tuple[Finding, ...]
    origin: ProposalOrigin
    #: Set when an Agent proposed it, so a reviewer can read the Run that did.
    origin_run_id: UUID | None
    status: ProposalStatus
    created_by: UUID
    created_at: datetime
    decided_by: UUID | None
    decided_at: datetime | None

    @property
    def approvable(self) -> bool:
        """§15.3 step 3: a blocking finding may be looked at, never approved."""
        return self.status is ProposalStatus.PENDING and not blocking(self.findings)
