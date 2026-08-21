"""§1 and §3, tied together: resolve who is asking into an `AuditScope`,
query with it, and — for exactly one of the five subjects — write down that
the question was asked at all.

Product design §4.6 (v2.5)'s 审计记录 row:

| 主体 | 可见范围 |
|---|---|
| 平台管理员 | 跨空间只读并留痕 |
| 工作空间管理员 | 本空间只读 |
| 开发者 | 与自己资源相关的只读 |
| 查看者 | 脱敏只读 |
| 终端用户 | 否 |

**"与自己资源相关的" — the decision this codebase makes.** Nothing in the
current schema tracks resource ownership (`agents`, `runs`, `sessions` all
lack an `owner_id`/`created_by` column), so "a resource this developer owns"
is not a fact any store here could look up. What *is* a fact, entirely
within `audit_events` itself, is which resources a developer has ever acted
on. A developer's own-resources scope is therefore: every row where they
are the actor, plus every row whose `resource_id` also appears on a row
where they are the actor — so a workspace administrator's later approval of
a Run this developer submitted stays visible to them, but a resource this
developer has never once touched, by any action, does not. The cost is a
developer occasionally missing a row about "their" resource that happened
before they ever acted on it (a Run seeded directly by a fixture, say); the
alternative — inventing an ownership model nowhere else in this codebase
enforces, just for this one read path — would be a decision this module has
no authority to make and no way to keep true as the real one evolves.

**Resolution mirrors `WorkspaceService._require_member` exactly**
(`tenancy/application/workspace_service.py`), for the same reason that
function already gives: an explicit membership row wins over the
platform-admin bypass, so a platform administrator who genuinely belongs to
this workspace reads it as that member — not as a cross-workspace visitor —
and only the branch with no membership row at all is what §4.6's "跨空间"
means and what gets logged.
"""

from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

from tiny_hermes.audit.domain.query import AuditFilter, AuditPage
from tiny_hermes.audit.domain.record import AuditRecord
from tiny_hermes.audit.domain.redaction import redact_context
from tiny_hermes.audit.domain.scope import AuditScope, AuditVisibility
from tiny_hermes.tenancy.domain.models import Actor, Role

#: §3 / §4.6's "留痕": what a platform administrator's cross-workspace read
#: of the audit trail writes about itself.
CROSS_WORKSPACE_READ = "audit.cross_workspace_read"


class AuditStore(Protocol):
    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def query(self, scope: AuditScope, filters: AuditFilter) -> AuditPage: ...

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        actor_type: str,
        action: str,
        resource_type: str,
        resource_id: UUID,
        request_id: str,
        context: dict[str, str] | None = None,
    ) -> None: ...


class AuditError(Exception):
    """Base for every expected refusal here."""


class ForbiddenAuditRead(AuditError):
    """§4.6 gives this row to five subjects, not six.

    Reached by an end user (the "否" cell, refused again here even though
    `_CONSOLE_ONLY` already refuses their cookie at the HTTP layer — this
    service must hold on its own if it is ever called some other way), a
    service account (§4.6's matrix names people, and audit visibility is
    not a Key scope anything could grant), or a console member with no
    workspace membership and no platform-admin flag. The message does not
    distinguish which — every other refusal in this codebase gives a
    caller who cannot see into a workspace nothing about why not.
    """


@dataclass(frozen=True)
class AuditService:
    store: AuditStore

    async def list_events(
        self, actor: Actor, workspace_id: UUID, filters: AuditFilter, request_id: str
    ) -> AuditPage:
        if actor.is_service_account or actor.is_end_user:
            raise ForbiddenAuditRead
        role = await self.store.user_role(workspace_id, actor.id)
        resolved = _scope_for(actor, role, workspace_id)
        if resolved is None:
            raise ForbiddenAuditRead
        scope, cross_workspace = resolved
        if cross_workspace:
            await self._log_cross_workspace_read(actor, workspace_id, filters, request_id)
        page = await self.store.query(scope, filters)
        if scope.visibility is AuditVisibility.REDACTED:
            page = AuditPage(
                items=tuple(_redacted(item) for item in page.items),
                has_more=page.has_more,
            )
        return page

    async def _log_cross_workspace_read(
        self, actor: Actor, workspace_id: UUID, filters: AuditFilter, request_id: str
    ) -> None:
        """One line per call, not per row and not per page.

        `design §6`'s own precedent (`docs/superpowers/specs/2026-08-20-
        end-user-entry-design.md`) draws the same line for session reads:
        a list of forty titles does not become forty audit rows, only the
        act of reading does. Here every call to this endpoint already *is*
        an act of reading — there is no separate "titles only" shape for
        audit rows the way there was for sessions — so each call that turns
        out to be cross-workspace writes exactly one line, including a
        platform administrator turning to a second page of the same view.
        """
        await self.store.append_audit(
            workspace_id=workspace_id,
            actor_id=actor.id,
            actor_type="user",
            action=CROSS_WORKSPACE_READ,
            resource_type="workspace",
            resource_id=workspace_id,
            request_id=request_id,
            context=_filter_context(filters),
        )


def _scope_for(
    actor: Actor, role: Role | None, workspace_id: UUID
) -> tuple[AuditScope, bool] | None:
    """`(scope, is_cross_workspace_platform_read)`, or `None` to refuse.

    The bool exists only for `list_events` to decide whether to log — it is
    never part of `AuditScope` itself (see that module's own docstring for
    why: cross-workspace bookkeeping is the application layer's decision
    about *this call*, not a wider scope the domain type could express).
    """
    if role is None:
        if not actor.is_platform_admin:
            return None
        return AuditScope.full(workspace_id=workspace_id), True
    if role is Role.WORKSPACE_ADMIN:
        return AuditScope.full(workspace_id=workspace_id), False
    if role is Role.DEVELOPER:
        return AuditScope.own_resources(workspace_id=workspace_id, actor_id=actor.id), False
    if role is Role.VIEWER:
        return AuditScope.redacted(workspace_id=workspace_id), False
    return None  # pragma: no cover - Role is exhaustive above


def _redacted(record: AuditRecord) -> AuditRecord:
    return replace(record, context=redact_context(record.context))


def _filter_context(filters: AuditFilter) -> dict[str, str]:
    """What a cross-workspace read's own audit line says it was looking
    for. Time is not repeated here — the row's own `created_at` already
    carries it, matching every other `append_audit` call in this codebase,
    none of which duplicates its own timestamp into `context`.
    """
    parts: dict[str, str] = {}
    if filters.action is not None:
        parts["action"] = filters.action
    if filters.resource_type is not None:
        parts["resource_type"] = filters.resource_type
    if filters.actor_id is not None:
        parts["actor_id"] = str(filters.actor_id)
    if filters.since is not None:
        parts["since"] = filters.since.isoformat()
    if filters.until is not None:
        parts["until"] = filters.until.isoformat()
    parts["limit"] = str(filters.limit)
    parts["offset"] = str(filters.offset)
    return parts
