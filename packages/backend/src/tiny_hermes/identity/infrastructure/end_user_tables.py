"""The third subject: somebody the platform does not authenticate.

Design §3. Three tables, and each one exists to keep a different promise from
§4.5.1 — **the platform is not an identity provider** — out of the code that
would otherwise have to remember it on its own.

`EndUserRow` carries no email, no name, nothing an enterprise's own directory
already owns. That is not a convention here; it is the reason §344's erasure
stays cheap (`erased_at`, one column, one table) instead of a search across
every place a person's details might have been copied. Whatever an enterprise
puts in a credential lands in `ExternalIdentityRow.profile` instead, which is
the row that is scoped to one channel and one workspace and can be cleared
without touching the subject the rest of the platform points at.

`ExternalIdentityRow` is the map from "who the enterprise says this is" to
"which stable subject that is here". `UNIQUE (workspace_id, channel,
external_user_id)` (§282) is what makes upserting on that triple safe: the same
`sub` from the same channel in the same workspace always resolves to the same
row, and two different channels never collide into one by accident — merging
identities across channels is a deliberate, unbuilt feature (design §10), not
an accident of a looser key.

`ChannelIssuerRow` answers two questions with one row because they are the
same sentence's two halves: which enterprise is this, and where is it allowed
to load from. Disabling a row is documented in §4.3 as taking effect for *new*
credentials only — already-exchanged sessions need the separate revocation
endpoint — so nothing here tries to make disabling instant everywhere; that
would be a promise this table cannot keep on its own.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tiny_hermes.identity.domain.models import ChannelIssuerStatus
from tiny_hermes.shared.database import Base, CreatedAtMixin, IdMixin


def _in_enum(column: str, values: type[StrEnum]) -> str:
    listed = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({listed})"


class EndUserRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "end_users"
    __table_args__ = (
        # Lets `ExternalIdentityRow` and later tables pin a foreign key to a
        # subject *within its own workspace*, the same composite-key pattern
        # `sessions` and `runs` already use to keep tenancy a property of the
        # schema rather than of every query that touches it.
        UniqueConstraint("id", "workspace_id", name="uq_end_users_id_workspace"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    #: Set once, never cleared. §3: the row survives its own erasure because
    #: `runs` and `sessions` reference it, and a Run is the platform's record
    #: that something happened — erasure removes what this subject is, not
    #: the fact that they were here.
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExternalIdentityRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "channel",
            "external_user_id",
            name="uq_external_identities_workspace_channel_external_user",
        ),
        ForeignKeyConstraint(
            ["end_user_id", "workspace_id"],
            ["end_users.id", "end_users.workspace_id"],
            name="fk_external_identities_end_user",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    #: `web`, `feishu`, and whatever else earns a credential path later. Not
    #: constrained to a fixed set here — a new channel is a new credential
    #: verifier and a new row in `channel_issuers`, not a schema migration.
    channel: Mapped[str] = mapped_column(String(32))
    #: The credential's `sub` (§4.1). Stable in the enterprise's own directory;
    #: meaningless without `channel` and `workspace_id` alongside it, which is
    #: why none of the three is unique on its own.
    external_user_id: Mapped[str] = mapped_column(String(255))
    end_user_id: Mapped[UUID] = mapped_column(index=True)
    #: Whatever the enterprise chose to put in the credential — a name, an
    #: email, anything identifying. Optional, and never read by anything that
    #: does not already know which workspace and channel it is asking about.
    profile: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class ChannelIssuerRow(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "channel_issuers"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "channel", "issuer", name="uq_channel_issuers_workspace_channel_issuer"
        ),
        CheckConstraint(_in_enum("status", ChannelIssuerStatus), name="ck_channel_issuers_status"),
        # A verifier needs a key from somewhere. Neither column is required on
        # its own — an issuer may rotate through a JWKS or hand over one fixed
        # key — but a row with neither is one nothing could ever verify against.
        CheckConstraint(
            "public_key IS NOT NULL OR jwks_url IS NOT NULL",
            name="ck_channel_issuers_key_source",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(32))
    #: The credential's `iss` (§4.1). Matched against this table's
    #: `(workspace_id, channel, issuer)` on every credential exchange.
    issuer: Mapped[str] = mapped_column(String(255))
    public_key: Mapped[str | None] = mapped_column(String, nullable=True)
    jwks_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    #: Origins this workspace allows to embed its chat surface (§7), reusing
    #: this row rather than a table of its own because who-may-embed and
    #: who-may-sign are the same trust decision made at the same time.
    allowed_origins: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default=ChannelIssuerStatus.ACTIVE.value)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
