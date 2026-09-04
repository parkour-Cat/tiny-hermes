"""Reading and writing `channel_bindings`.

Kept apart from `SqlChannelStore`, which is the inbound delivery path and
runs inside a webhook. This one runs inside a console request, and the two
have no queries in common.
"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tiny_hermes.agents.infrastructure.tables import AgentRow
from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.channels.application.binding_service import ChannelBindingView
from tiny_hermes.channels.infrastructure.tables import ChannelBindingRow
from tiny_hermes.secrets.infrastructure.tables import SecretRow
from tiny_hermes.tenancy.domain.models import Role
from tiny_hermes.tenancy.infrastructure.tables import MembershipRow


class SqlChannelBindingStore:
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

    async def secret_exists(self, workspace_id: UUID, reference: str) -> bool:
        """By **id**, because that is what resolves it later.

        `CredentialResolver` reads a Secret id, or else an
        environment-variable name; a secret's *name* is neither. This used to
        check the name, so a binding validated cleanly and then failed at the
        first real delivery with `CredentialMissing` — validation and use
        were asking different questions, and only the second one counted.

        Scoped to this workspace on purpose: a platform-scoped secret would
        resolve at delivery time, but naming one here would let a workspace
        administrator bind a channel to a secret they cannot see.
        """
        try:
            secret_id = UUID(reference)
        except ValueError:
            return False
        found = await self._session.scalar(
            select(SecretRow.id).where(
                SecretRow.workspace_id == workspace_id,
                SecretRow.id == secret_id,
                SecretRow.status == "active",
            )
        )
        return found is not None

    async def agent_exists(self, workspace_id: UUID, agent_id: UUID) -> bool:
        found = await self._session.scalar(
            select(AgentRow.id).where(
                AgentRow.id == agent_id, AgentRow.workspace_id == workspace_id
            )
        )
        return found is not None

    async def create_binding(
        self,
        *,
        workspace_id: UUID,
        channel: str,
        agent_id: UUID,
        created_by: UUID,
        app_id: str | None,
        encrypt_key_ref: str | None,
        app_secret_ref: str | None,
    ) -> ChannelBindingView | None:
        row = ChannelBindingRow(
            workspace_id=workspace_id,
            channel=channel,
            agent_id=agent_id,
            status="active",
            created_by=created_by,
            app_id=app_id,
            encrypt_key_ref=encrypt_key_ref,
            app_secret_ref=app_secret_ref,
        )
        self._session.add(row)
        try:
            # Flushed here rather than left to the request's commit: the
            # unique constraint is how "already bound" is decided, and a
            # violation surfacing at commit would land after the audit row
            # had already been written for a binding that does not exist.
            await self._session.flush()
        except IntegrityError:
            await self._session.rollback()
            return None
        return _view(row)

    async def list_bindings(self, workspace_id: UUID) -> tuple[ChannelBindingView, ...]:
        rows = (
            await self._session.scalars(
                select(ChannelBindingRow)
                .where(ChannelBindingRow.workspace_id == workspace_id)
                .order_by(ChannelBindingRow.created_at, ChannelBindingRow.id)
            )
        ).all()
        return tuple(_view(row) for row in rows)

    async def binding(
        self, workspace_id: UUID, binding_id: UUID
    ) -> ChannelBindingView | None:
        row = await self._session.scalar(
            select(ChannelBindingRow).where(
                ChannelBindingRow.id == binding_id,
                ChannelBindingRow.workspace_id == workspace_id,
            )
        )
        return None if row is None else _view(row)

    async def update_binding(
        self,
        workspace_id: UUID,
        binding_id: UUID,
        changes: dict[str, str | None],
    ) -> ChannelBindingView | None:
        """Only the columns named in `changes`, and nothing else.

        The caller decides what "named" means — a PATCH that could not tell
        an absent field from an explicit null would either strip the encrypt
        key on every update or make a receive-only binding unreachable. That
        decision belongs at the route, where the request body is, so this
        takes a settled mapping rather than a pile of optionals.
        """
        if not changes:
            return await self.binding(workspace_id, binding_id)
        updated = (
            await self._session.execute(
                update(ChannelBindingRow)
                .where(
                    ChannelBindingRow.id == binding_id,
                    ChannelBindingRow.workspace_id == workspace_id,
                )
                .values(**changes)
                .returning(ChannelBindingRow)
            )
        ).scalar_one_or_none()
        return None if updated is None else _view(updated)

    async def disable_binding(
        self, workspace_id: UUID, binding_id: UUID
    ) -> ChannelBindingView | None:
        # `workspace_id` in the where-clause, not checked after the read: an
        # update that found the row first and filtered afterwards would have
        # already touched a row belonging to somebody else.
        updated = (
            await self._session.execute(
                update(ChannelBindingRow)
                .where(
                    ChannelBindingRow.id == binding_id,
                    ChannelBindingRow.workspace_id == workspace_id,
                )
                .values(status="disabled")
                .returning(ChannelBindingRow)
            )
        ).scalar_one_or_none()
        return None if updated is None else _view(updated)

    async def append_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        request_id: str,
    ) -> None:
        self._session.add(
            AuditEventRow(
                workspace_id=workspace_id,
                actor_type="user",
                actor_id=actor_id,
                action=action,
                resource_type="channel_binding",
                resource_id=resource_id,
                result="succeeded",
                request_id=request_id,
                context={},
            )
        )


def _view(row: ChannelBindingRow) -> ChannelBindingView:
    """No key, and nowhere to put one — §4.6's `不查看明文` is structural
    here rather than a field somebody has to remember to leave out."""
    return ChannelBindingView(
        id=row.id,
        workspace_id=row.workspace_id,
        channel=row.channel,
        agent_id=row.agent_id,
        status=row.status,
        app_id=row.app_id,
        encrypt_key_ref=row.encrypt_key_ref,
        app_secret_ref=row.app_secret_ref,
        transport=row.transport,
        long_connection_seen_at=row.long_connection_seen_at,
        created_by=row.created_by,
        created_at=row.created_at,
    )
