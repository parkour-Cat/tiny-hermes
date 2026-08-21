"""What `EndUserIdentityService` needs from storage, and nothing it does not.

Two unrelated concerns share one store rather than two, the way
`SqlScopeStore` backs both platform- and workspace-level `OutboundScopes`
writes: registering a `channel_issuers` row and exchanging a credential both
end up reading and writing the same three tables (design §3), and a second
store would just be a second place to keep their queries consistent with
each other.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tiny_hermes.identity.domain.models import ChannelIssuerStatus
from tiny_hermes.tenancy.domain.models import Role


class IssuerAlreadyRegistered(Exception):
    """`UNIQUE (workspace_id, channel, issuer)` already has this row."""


@dataclass(frozen=True)
class ChannelIssuerRecord:
    id: UUID
    workspace_id: UUID
    channel: str
    issuer: str
    public_key: str | None
    jwks_url: str | None
    allowed_origins: tuple[str, ...]
    status: ChannelIssuerStatus
    created_by: UUID
    created_at: datetime


@dataclass(frozen=True)
class UpsertedIdentity:
    """§282's upsert, resolved: the subject this credential's `sub` maps to,
    and whether it survives only as a record that it once existed."""

    end_user_id: UUID
    erased_at: datetime | None


@dataclass(frozen=True)
class StoredEndUserSession:
    """Never returned for an expired or revoked row — see `find_session`."""

    end_user_id: UUID
    workspace_id: UUID
    #: The credential's own `agents` claim, copied in at exchange time. See
    #: `EndUserSessionRow.agents` for why this row is where it has to live.
    agents: tuple[str, ...] = ()
    #: Which `channel_issuers` row minted this session (§7, task-7 review
    #: finding 3), or `None` for a session written before that column
    #: existed. See `EndUserSessionRow.channel_issuer_id`.
    channel_issuer_id: UUID | None = None


class EndUserStore(Protocol):
    async def user_role(self, workspace_id: UUID, user_id: UUID) -> Role | None: ...

    async def create_issuer(
        self,
        *,
        workspace_id: UUID,
        channel: str,
        issuer: str,
        public_key: str | None,
        jwks_url: str | None,
        allowed_origins: Sequence[str],
        created_by: UUID,
    ) -> ChannelIssuerRecord: ...

    async def list_issuers(self, workspace_id: UUID) -> Sequence[ChannelIssuerRecord]: ...

    async def disable_issuer(
        self, workspace_id: UUID, issuer_id: UUID
    ) -> ChannelIssuerRecord | None: ...

    async def find_issuer(self, workspace_id: UUID, issuer: str) -> ChannelIssuerRecord | None:
        """Scoped by `(workspace_id, issuer)`, not by channel.

        Design §4.1 puts no channel claim in the credential itself, so at
        exchange time channel is not yet known — it is *read off* whichever
        row's `issuer` matches, not used to find that row. A workspace that
        registered the same issuer string under two channels makes that
        string ambiguous; this returns `None` for it rather than guessing,
        which routes it into the same fixed-cost refusal path as an issuer
        nobody registered at all (§8).
        """
        ...

    async def upsert_external_identity(
        self, workspace_id: UUID, channel: str, external_user_id: str
    ) -> UpsertedIdentity: ...

    async def create_session(
        self,
        end_user_id: UUID,
        workspace_id: UUID,
        token_digest: str,
        expires_at: datetime,
        agents: Sequence[str],
        channel_issuer_id: UUID,
    ) -> None: ...

    async def find_session(
        self, token_digest: str, now: datetime
    ) -> StoredEndUserSession | None: ...

    async def revoke_sessions(self, end_user_id: UUID, workspace_id: UUID, now: datetime) -> None:
        """Every not-yet-revoked session for this subject, in this workspace.

        Design §4.3's immediate half of the two-part revocation story: the
        other half, a disabled `channel_issuers` row, only stops *new*
        credentials and is handled by `disable_issuer`. Idempotent — revoking
        a subject with no active sessions is not an error, so there is
        nothing for a caller to do with a count.
        """
        ...

    async def end_user_exists(self, workspace_id: UUID, end_user_id: UUID) -> bool: ...

    async def allowed_origins_for_issuer(
        self, workspace_id: UUID, channel_issuer_id: UUID
    ) -> frozenset[str]:
        """`allowed_origins` for one `channel_issuers` row — the one that
        minted the session a write is being checked against (design §7,
        task-7 review finding 3) — never unioned across every issuer the
        workspace happens to have registered. `EndUserIdentityService`
        already resolved "does this session even have a recorded issuer"
        before calling this, so `channel_issuer_id` here is always a real
        id to look up, not `None`. A disabled issuer's row still returns
        empty, same as it does for new credential exchanges (`find_issuer`)
        — an origin only a disabled row still names is not one this
        workspace currently vouches for.
        """
        ...

    async def append_audit(
        self,
        *,
        workspace_id: UUID | None,
        actor_type: str,
        actor_id: UUID | None,
        action: str,
        resource_type: str,
        resource_id: UUID | None,
        request_id: str,
        context: dict[str, str],
    ) -> None: ...
