"""Who may read which rows of `audit_events`, and the question this module
refuses to answer.

Product design §4.6 (v2.5)'s 审计记录 row gives five subjects five different
ranges — 跨空间只读并留痕、本空间只读、与自己资源相关的只读、脱敏只读、否 — not
one switch. `AuditScope` is the shape that keeps those five from collapsing
into a boolean: it names exactly one workspace and one of three visibilities
within it, and nothing this module exports can widen that past a single
workspace.

**There is no way to express "every workspace's every row".** Not forbidden
— unwriteable, the same technique `MemoryScope` uses and for the same
reason (`memory/domain/scope.py`'s own docstring): the widest range a caller
can construct is the range somebody eventually gets, so "all workspaces" is
never on the menu, not even behind a platform-administrator branch. A
platform administrator's cross-workspace reach (§4.6's "跨空间只读并留痕")
is real, but it is the *application* layer asking for one workspace's `FULL`
scope once per read and deciding whether to log it — never a wider scope
than a workspace admin's own.

`OWN_RESOURCES` and `REDACTED` are their own visibilities rather than a
`FULL` scope plus a flag, mirroring `MemoryScope.shared()`'s own reasoning:
a call site that wants "restricted to this actor" or "redact on the way out"
has to say so by picking a different constructor, not by remembering to set
an extra field on the wide one.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class AuditVisibility(StrEnum):
    #: Every row in the workspace, `context` untouched. Workspace admins and
    #: platform admins both land here — §4.6 gives both "只读" over the whole
    #: trail; only whether the read gets logged differs, and that is not a
    #: property of the scope (see the module docstring).
    FULL = "full"
    #: Rows this actor is implicated in. See `AuditService` for the exact
    #: definition of "implicated" this codebase chose — it is a decision,
    #: not something this module could derive from the word "own" alone.
    OWN_RESOURCES = "own_resources"
    #: Every row in the workspace, but `context` is redacted on the way out
    #: (`audit/domain/redaction.py`). Row-level access is the same as `FULL`
    #: — §2 redacts a column, not a set of rows.
    REDACTED = "redacted"


@dataclass(frozen=True)
class AuditScope:
    """Exactly one workspace, exactly one visibility within it.

    Frozen and complete, like `MemoryScope`: there is no fourth visibility
    and no partial one. `workspace_id` is required by every constructor and
    checked again in `__post_init__` rather than trusted to the type
    annotation alone — a plain dataclass does not enforce its own hints at
    runtime, and the one caller a type checker cannot stop is a `# type:
    ignore` away from building the wildcard this module exists to prevent.
    """

    workspace_id: UUID
    visibility: AuditVisibility
    #: `None` unless `visibility` is `OWN_RESOURCES`, in which case it is
    #: whose resources — enforced below for the same reason `MemoryScope`
    #: enforces `subject`'s presence: an own-resources scope with nobody
    #: named, and a full scope narrowed by an actor nobody asked to narrow
    #: by, are both silent mistakes.
    actor_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.workspace_id is None:  # type: ignore[comparison-overlap]
            raise ValueError("an audit scope names exactly one workspace")
        needs_actor = self.visibility is AuditVisibility.OWN_RESOURCES
        if needs_actor and self.actor_id is None:
            raise ValueError("an own-resources scope needs whose resources")
        if not needs_actor and self.actor_id is not None:
            raise ValueError("only an own-resources scope narrows by actor")

    @classmethod
    def full(cls, *, workspace_id: UUID) -> "AuditScope":
        return cls(workspace_id=workspace_id, visibility=AuditVisibility.FULL)

    @classmethod
    def own_resources(cls, *, workspace_id: UUID, actor_id: UUID) -> "AuditScope":
        return cls(
            workspace_id=workspace_id,
            visibility=AuditVisibility.OWN_RESOURCES,
            actor_id=actor_id,
        )

    @classmethod
    def redacted(cls, *, workspace_id: UUID) -> "AuditScope":
        return cls(workspace_id=workspace_id, visibility=AuditVisibility.REDACTED)
