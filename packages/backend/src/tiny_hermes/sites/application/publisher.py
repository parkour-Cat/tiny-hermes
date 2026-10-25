"""Publishing a session's committed files as a site, and serving them (§1.26, v2.26)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.session_workspace.application.files import (
    WorkspaceFileNotFound,
    WorkspaceFiles,
)
from tiny_hermes.session_workspace.infrastructure.sql_files import SqlWorkspaceFiles
from tiny_hermes.session_workspace.ports.objects import ObjectStore
from tiny_hermes.sites.domain.site import (
    SiteRefused,
    media_type,
    new_token,
    select_site,
    token_digest,
)
from tiny_hermes.sites.infrastructure.tables import SitePublicationRow, SiteTicketUseRow

#: However often it is extended, a link lives at most this long after it was made.
MAX_LIFETIME = timedelta(days=90)
VISIBILITIES = ("members", "link")


class SitesDisabled(Exception):
    """This deployment has no site origin, so nothing can be published."""


class SiteFileNotFound(Exception):
    pass


@dataclass(frozen=True)
class SiteSettings:
    public_url: str | None
    signing_secret: str | None
    ttl_days: int = 7


@dataclass(frozen=True)
class Published:
    publication_id: UUID
    url: str
    expires_at: datetime
    visibility: str


@dataclass(frozen=True)
class ActiveSite:
    id: UUID
    workspace_id: UUID
    session_id: UUID
    revision_id: UUID
    root: str
    visibility: str
    expires_at: datetime


@dataclass(frozen=True)
class SiteView:
    id: UUID
    run_id: UUID
    title: str | None
    root: str
    visibility: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class SitePublisher:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        objects: ObjectStore,
        settings: SiteSettings,
    ) -> None:
        self._sessions = sessions
        self._objects = objects
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.public_url)

    async def publish(
        self,
        *,
        workspace_id: UUID,
        session_id: UUID,
        run_id: UUID,
        root: str,
        title: str | None,
    ) -> Published:
        """The session's latest committed revision under `root`, as a new link."""
        if not self.settings.public_url:
            raise SitesDisabled
        now = datetime.now(UTC)
        token = new_token()
        async with self._sessions.begin() as db:
            files = WorkspaceFiles(SqlWorkspaceFiles(db), self._objects)
            source = await files.store.session_source(workspace_id, session_id)
            snapshot = await files.list_files(source)
            if snapshot.revision_id is None:
                raise SiteRefused("not_found", "nothing has been saved in this workspace yet")
            select_site(snapshot.items, root)
            row = SitePublicationRow(
                id=uuid4(),
                created_at=now,
                workspace_id=workspace_id,
                session_id=session_id,
                run_id=run_id,
                revision_id=snapshot.revision_id,
                root=root.strip().strip("/") or ".",
                title=(title or None) and title[:100],
                token_sha256=token_digest(token),
                visibility="members",
                expires_at=now + timedelta(days=self.settings.ttl_days),
            )
            db.add(row)
            _audit(
                db,
                workspace_id,
                row.id,
                "site.published",
                actor_type="run",
                actor_id=run_id,
                request_id=f"run:{run_id}",
                context={
                    "run_id": str(run_id),
                    "revision_id": str(snapshot.revision_id),
                    "root": row.root,
                },
            )
        base = self.settings.public_url.rstrip("/")
        return Published(row.id, f"{base}/s/{token}/", row.expires_at, row.visibility)

    async def resolve(self, token: str) -> ActiveSite | None:
        """A link's site, if it is neither revoked nor expired."""
        async with self._sessions() as db:
            row = await db.scalar(
                select(SitePublicationRow).where(
                    SitePublicationRow.token_sha256 == token_digest(token)
                )
            )
        if row is None or row.revoked_at is not None or row.expires_at <= datetime.now(UTC):
            return None
        return ActiveSite(
            row.id,
            row.workspace_id,
            row.session_id,
            row.revision_id,
            row.root,
            row.visibility,
            row.expires_at,
        )

    async def open_file(
        self, site: ActiveSite, path: str
    ) -> tuple[str, int, AsyncIterator[bytes]]:
        """One file of the pinned snapshot: its media type, size and bytes."""
        wanted = path or "index.html"
        if wanted.endswith("/"):
            wanted += "index.html"
        async with self._sessions() as db:
            files = WorkspaceFiles(SqlWorkspaceFiles(db), self._objects)
            source = await SqlWorkspaceFiles(db).revision_source(
                site.workspace_id, site.session_id, site.revision_id
            )
            snapshot = await files.list_files(source)
            entry = select_site(snapshot.items, site.root).get(wanted)
            if entry is None:
                raise SiteFileNotFound(wanted)
            try:
                _, stream = await files.content(source, site.revision_id, entry.path)
            except WorkspaceFileNotFound as missing:
                raise SiteFileNotFound(wanted) from missing
        return media_type(wanted), entry.size, stream

    async def list(self, workspace_id: UUID) -> list[SiteView]:
        async with self._sessions() as db:
            rows = (
                await db.scalars(
                    select(SitePublicationRow)
                    .where(SitePublicationRow.workspace_id == workspace_id)
                    .order_by(SitePublicationRow.created_at.desc(), SitePublicationRow.id)
                )
            ).all()
        return [
            SiteView(
                row.id,
                row.run_id,
                row.title,
                row.root,
                row.visibility,
                row.created_at,
                row.expires_at,
                row.revoked_at,
            )
            for row in rows
        ]

    async def revoke(
        self, workspace_id: UUID, publication_id: UUID, *, actor_id: UUID | None, request_id: str
    ) -> bool:
        async with self._sessions.begin() as db:
            done = await db.execute(
                update(SitePublicationRow)
                .where(
                    SitePublicationRow.id == publication_id,
                    SitePublicationRow.workspace_id == workspace_id,
                    SitePublicationRow.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC))
                .returning(SitePublicationRow.id)
            )
            if done.scalar_one_or_none() is None:
                return False
            _audit(db, workspace_id, publication_id, "site.revoked", actor_id=actor_id,
                   request_id=request_id)
        return True

    async def extend(
        self, workspace_id: UUID, publication_id: UUID, *, actor_id: UUID | None, request_id: str
    ) -> datetime | None:
        """A fresh period from now, never past ninety days after publishing."""
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            row = await db.scalar(
                select(SitePublicationRow)
                .where(
                    SitePublicationRow.id == publication_id,
                    SitePublicationRow.workspace_id == workspace_id,
                    SitePublicationRow.revoked_at.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                return None
            row.expires_at = min(
                now + timedelta(days=self.settings.ttl_days), row.created_at + MAX_LIFETIME
            )
            _audit(db, workspace_id, publication_id, "site.extended", actor_id=actor_id,
                   request_id=request_id, context={"expires_at": row.expires_at.isoformat()})
            return row.expires_at

    async def set_visibility(
        self,
        workspace_id: UUID,
        publication_id: UUID,
        visibility: str,
        *,
        actor_id: UUID | None,
        request_id: str,
    ) -> bool:
        if visibility not in VISIBILITIES:
            raise ValueError(f"visibility must be one of {VISIBILITIES}")
        async with self._sessions.begin() as db:
            done = await db.execute(
                update(SitePublicationRow)
                .where(
                    SitePublicationRow.id == publication_id,
                    SitePublicationRow.workspace_id == workspace_id,
                    SitePublicationRow.revoked_at.is_(None),
                )
                .values(visibility=visibility)
                .returning(SitePublicationRow.id)
            )
            if done.scalar_one_or_none() is None:
                return False
            _audit(db, workspace_id, publication_id, "site.visibility_changed",
                   actor_id=actor_id, request_id=request_id, context={"visibility": visibility})
        return True

    async def use_ticket_once(self, nonce: str) -> bool:
        """True the first time a ticket is exchanged, False ever after."""
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            # A ticket lives a minute; a day of history is far more than enough.
            await db.execute(
                delete(SiteTicketUseRow).where(SiteTicketUseRow.used_at < now - timedelta(days=1))
            )
            inserted = await db.execute(
                insert(SiteTicketUseRow)
                .values(nonce=nonce[:64], used_at=now)
                .on_conflict_do_nothing(index_elements=["nonce"])
                .returning(SiteTicketUseRow.nonce)
            )
            return inserted.scalar_one_or_none() is not None


def _audit(
    db: AsyncSession,
    workspace_id: UUID,
    publication_id: UUID,
    action: str,
    *,
    actor_id: UUID | None,
    request_id: str,
    actor_type: str = "user",
    context: dict[str, str] | None = None,
) -> None:
    db.add(
        AuditEventRow(
            id=uuid4(),
            workspace_id=workspace_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            resource_type="site",
            resource_id=publication_id,
            result="succeeded",
            request_id=request_id[:80],
            context=context or {},
        )
    )
