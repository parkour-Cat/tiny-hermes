"""The platform's own credential for the third subject.

Design §4.2–§4.3. An enterprise-signed JWT proves who an end user is exactly
once, at exchange; after that this table is the only thing that matters. It
is deliberately not `auth_sessions`: design §3's red line is that the two
identity systems never share a credential, and a shared table is a shared
credential in every way that counts — one migration touching both, one query
that could join across them by accident, one column rename that moves for
the wrong subject. Two tables make "these are separate systems" a property of
the schema rather than a promise about how the code that reads it behaves.

`token_digest` rather than the token itself, for the same reason
`AuthSessionRow` never stores one: a session store is a thing worth reading
from a backup, and a bearer credential is not a thing worth being in one.

`end_user_id` and `workspace_id` are FK'd together against
`(end_users.id, end_users.workspace_id)`, the same composite pattern
`ExternalIdentityRow` uses — it is what lets the revocation endpoint
(`DELETE /api/v1/end-user/sessions/{end_user_id}`) trust that an end user id
scoped to one workspace can never be handed a session row that belongs to
another.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


class EndUserSessionRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "end_user_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["end_user_id", "workspace_id"],
            ["end_users.id", "end_users.workspace_id"],
            name="fk_end_user_sessions_end_user",
        ),
    )

    end_user_id: Mapped[UUID] = mapped_column(index=True)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    #: Independent of the credential that was exchanged for it (design §4.2):
    #: the credential's own `exp` stopped mattering the moment this row was
    #: written. Default 8 hours, enforced by the caller at issuance.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Set by `DELETE /api/v1/end-user/sessions/{end_user_id}` (design §4.3):
    #: the workspace admin's way to end a session immediately, independent of
    #: `expires_at` and independent of whatever `channel_issuers.status` says.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
