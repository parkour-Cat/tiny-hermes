"""Publishing a session's committed files as a site (§1.26, v2.26), on real storage."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.audit.infrastructure.tables import AuditEventRow
from tiny_hermes.runs.infrastructure.tables import RunRow
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.shared.config import Settings
from tiny_hermes.sites.application.publisher import (
    SiteFileNotFound,
    SitePublisher,
    SitesDisabled,
    SiteSettings,
)
from tiny_hermes.sites.domain.site import SiteRefused
from tiny_hermes.sites.infrastructure.tables import SitePublicationRow
from tiny_hermes.tenancy.infrastructure.tables import WorkspaceRow

from ..workspace_file_support import seed_site

PUBLIC = "http://127.0.0.1:8090"
SECRET = "a-site-signing-secret-of-32-chars!"  # noqa: S105 - a test value
INDEX = b"<!doctype html><title>t</title><script src=app.js></script>"
APP = b"document.title = 'hello';"


def _publisher(engine: AsyncEngine, settings: Settings, **overrides: Any) -> SitePublisher:
    site: dict[str, Any] = {
        "public_url": PUBLIC,
        "signing_secret": SECRET,
        "ttl_days": 7,
        **overrides,
    }
    return SitePublisher(
        sessions=async_sessionmaker(engine, expire_on_commit=False),
        objects=MinioObjectStore(
            endpoint=settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            bucket=settings.s3_bucket,
        ),
        settings=SiteSettings(**site),
    )


async def _ids(engine: AsyncEngine, run_id: str) -> tuple[UUID, UUID, UUID]:
    async with engine.connect() as db:
        row = (await db.execute(select(RunRow).where(RunRow.id == UUID(run_id)))).one()
    return row.workspace_id, row.session_id, row.id


async def _read(publisher: SitePublisher, token: str, path: str) -> bytes:
    found = await publisher.resolve(token)
    assert found is not None
    _, _, stream = await publisher.open_file(found, path)
    return b"".join([chunk async for chunk in stream])


def _token(url: str) -> str:
    assert url.startswith(f"{PUBLIC}/s/") and url.endswith("/")
    return url[len(f"{PUBLIC}/s/"):-1]


async def test_a_session_s_committed_site_is_published_and_served_from_its_link(
    engine: AsyncEngine, settings: Settings, submitted_run: dict[str, Any]
) -> None:
    run_id = str(submitted_run["id"])
    await seed_site(
        engine, settings, run_id, {"site/index.html": INDEX, "site/app.js": APP, "x.sh": b"rm"}
    )
    workspace, session, run = await _ids(engine, run_id)
    publisher = _publisher(engine, settings)

    published = await publisher.publish(
        workspace_id=workspace, session_id=session, run_id=run, root="site", title="Hello"
    )

    token = _token(published.url)
    assert published.visibility == "members"
    assert abs(published.expires_at - (datetime.now(UTC) + timedelta(days=7))) < timedelta(
        minutes=1
    )
    assert await _read(publisher, token, "") == INDEX
    assert await _read(publisher, token, "app.js") == APP
    found = await publisher.resolve(token)
    assert found is not None
    media, size, _ = await publisher.open_file(found, "app.js")
    assert media == "text/javascript; charset=utf-8" and size == len(APP)
    with pytest.raises(SiteFileNotFound):
        await publisher.open_file(found, "../x.sh")
    with pytest.raises(SiteFileNotFound):
        await publisher.open_file(found, "missing.css")


async def test_the_link_keeps_the_snapshot_it_was_published_from(
    engine: AsyncEngine, settings: Settings, submitted_run: dict[str, Any]
) -> None:
    run_id = str(submitted_run["id"])
    await seed_site(engine, settings, run_id, {"site/index.html": INDEX})
    workspace, session, run = await _ids(engine, run_id)
    publisher = _publisher(engine, settings)
    published = await publisher.publish(
        workspace_id=workspace, session_id=session, run_id=run, root="site", title=None
    )

    await seed_site(engine, settings, run_id, {"site/index.html": b"<p>changed later</p>"})

    assert await _read(publisher, _token(published.url), "index.html") == INDEX


async def test_a_directory_that_is_not_a_site_is_refused_with_its_reason(
    engine: AsyncEngine, settings: Settings, submitted_run: dict[str, Any]
) -> None:
    run_id = str(submitted_run["id"])
    await seed_site(engine, settings, run_id, {"site/main.html": INDEX})
    workspace, session, run = await _ids(engine, run_id)

    with pytest.raises(SiteRefused) as refused:
        await _publisher(engine, settings).publish(
            workspace_id=workspace, session_id=session, run_id=run, root="site", title=None
        )
    assert refused.value.reason == "no_index"


async def test_nothing_is_published_where_the_deployment_has_no_site_origin(
    engine: AsyncEngine, settings: Settings, submitted_run: dict[str, Any]
) -> None:
    run_id = str(submitted_run["id"])
    await seed_site(engine, settings, run_id, {"site/index.html": INDEX})
    workspace, session, run = await _ids(engine, run_id)

    with pytest.raises(SitesDisabled):
        await _publisher(engine, settings, public_url=None).publish(
            workspace_id=workspace, session_id=session, run_id=run, root="site", title=None
        )


async def test_a_revoked_or_expired_link_resolves_to_nothing(
    engine: AsyncEngine, settings: Settings, submitted_run: dict[str, Any]
) -> None:
    run_id = str(submitted_run["id"])
    await seed_site(engine, settings, run_id, {"site/index.html": INDEX})
    workspace, session, run = await _ids(engine, run_id)
    publisher = _publisher(engine, settings)
    first = await publisher.publish(
        workspace_id=workspace, session_id=session, run_id=run, root="site", title=None
    )
    second = await publisher.publish(
        workspace_id=workspace, session_id=session, run_id=run, root="site", title=None
    )

    assert await publisher.revoke(workspace, first.publication_id, actor_id=None, request_id="r")
    async with engine.begin() as db:
        await db.execute(
            update(SitePublicationRow)
            .where(SitePublicationRow.id == second.publication_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    assert await publisher.resolve(_token(first.url)) is None
    assert await publisher.resolve(_token(second.url)) is None
    assert await publisher.resolve("not-a-token") is None


async def test_erasing_the_workspace_takes_its_sites_with_it(
    engine: AsyncEngine, settings: Settings, submitted_run: dict[str, Any]
) -> None:
    """Erasure wins over sharing: the revision rows go, and the links with them."""
    run_id = str(submitted_run["id"])
    await seed_site(engine, settings, run_id, {"site/index.html": INDEX})
    workspace, session, run = await _ids(engine, run_id)
    publisher = _publisher(engine, settings)
    published = await publisher.publish(
        workspace_id=workspace, session_id=session, run_id=run, root="site", title=None
    )

    async with engine.begin() as db:
        await db.execute(delete(WorkspaceRow).where(WorkspaceRow.id == workspace))

    assert await publisher.resolve(_token(published.url)) is None


async def test_extending_starts_a_fresh_period_and_stops_at_ninety_days(
    engine: AsyncEngine, settings: Settings, submitted_run: dict[str, Any]
) -> None:
    run_id = str(submitted_run["id"])
    await seed_site(engine, settings, run_id, {"site/index.html": INDEX})
    workspace, session, run = await _ids(engine, run_id)
    publisher = _publisher(engine, settings)
    published = await publisher.publish(
        workspace_id=workspace, session_id=session, run_id=run, root="site", title=None
    )

    extended = await publisher.extend(
        workspace, published.publication_id, actor_id=None, request_id="r"
    )
    assert extended is not None
    assert abs(extended - (datetime.now(UTC) + timedelta(days=7))) < timedelta(minutes=1)

    async with engine.begin() as db:
        await db.execute(
            update(SitePublicationRow)
            .where(SitePublicationRow.id == published.publication_id)
            .values(created_at=datetime.now(UTC) - timedelta(days=88))
        )
    capped = await publisher.extend(
        workspace, published.publication_id, actor_id=None, request_id="r"
    )
    assert capped is not None
    assert abs(capped - (datetime.now(UTC) + timedelta(days=2))) < timedelta(minutes=1)


async def test_publishing_and_revoking_are_audited_with_the_run(
    engine: AsyncEngine, settings: Settings, submitted_run: dict[str, Any]
) -> None:
    run_id = str(submitted_run["id"])
    await seed_site(engine, settings, run_id, {"site/index.html": INDEX})
    workspace, session, run = await _ids(engine, run_id)
    publisher = _publisher(engine, settings)
    published = await publisher.publish(
        workspace_id=workspace, session_id=session, run_id=run, root="site", title=None
    )
    await publisher.set_visibility(
        workspace, published.publication_id, "link", actor_id=None, request_id="r"
    )
    await publisher.revoke(workspace, published.publication_id, actor_id=None, request_id="r")

    async with engine.connect() as db:
        rows = (
            await db.execute(
                select(AuditEventRow.action, AuditEventRow.context)
                .where(AuditEventRow.resource_id == published.publication_id)
                .order_by(AuditEventRow.created_at)
            )
        ).all()
    assert [row.action for row in rows] == [
        "site.published",
        "site.visibility_changed",
        "site.revoked",
    ]
    assert rows[0].context["run_id"] == str(run)


async def test_a_ticket_is_good_for_one_use(engine: AsyncEngine, settings: Settings) -> None:
    """The members' door hands out a one-minute ticket; replaying it opens nothing."""
    publisher = _publisher(engine, settings)

    assert await publisher.use_ticket_once("nonce-1") is True
    assert await publisher.use_ticket_once("nonce-1") is False
    assert await publisher.use_ticket_once("nonce-2") is True
