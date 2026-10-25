"""The site origin itself (§1.26, v2.26): what it serves, with which headers,
and who it lets in."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.infrastructure.tables import RunRow
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.shared.config import Settings
from tiny_hermes.sites.application.publisher import Published, SitePublisher, SiteSettings
from tiny_hermes.sites.domain.site import sign
from tiny_hermes.sites.server import create_site_app

from ..workspace_file_support import seed_site
from .conftest import CONSOLE_URL, SITE_PUBLIC_URL, SITE_SECRET

INDEX = b"<!doctype html><h1>Ledger</h1>"
APP = b"console.log('hi')"


def _publisher(engine: AsyncEngine, settings: Settings) -> SitePublisher:
    return SitePublisher(
        sessions=async_sessionmaker(engine, expire_on_commit=False),
        objects=MinioObjectStore(
            endpoint=settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            bucket=settings.s3_bucket,
        ),
        settings=SiteSettings(SITE_PUBLIC_URL, SITE_SECRET, 7),
    )


async def _publish(
    engine: AsyncEngine, settings: Settings, run_id: str, visibility: str = "members"
) -> Published:
    await seed_site(engine, settings, run_id, {"site/index.html": INDEX, "site/js/app.js": APP})
    async with engine.connect() as db:
        run = (await db.execute(select(RunRow).where(RunRow.id == UUID(run_id)))).one()
    publisher = _publisher(engine, settings)
    published = await publisher.publish(
        workspace_id=run.workspace_id,
        session_id=run.session_id,
        run_id=run.id,
        root="site",
        title=None,
    )
    if visibility != "members":
        await publisher.set_visibility(
            run.workspace_id, published.publication_id, visibility, actor_id=None, request_id="t"
        )
    return published


def _path(published: Published) -> str:
    return published.url.removeprefix(SITE_PUBLIC_URL)


def _ticket(publication_id: UUID, *, seconds: int = 60, nonce: str | None = None) -> str:
    return sign(
        SITE_SECRET,
        "ticket",
        {"p": str(publication_id), "u": str(uuid4()), "n": nonce or uuid4().hex},
        datetime.now(UTC) + timedelta(seconds=seconds),
    )


@pytest.fixture
def site(engine: AsyncEngine, settings: Settings) -> TestClient:
    return TestClient(
        create_site_app(settings, publisher=_publisher(engine, settings)),
        base_url=SITE_PUBLIC_URL,
    )


async def test_a_site_open_to_anyone_is_served_with_its_own_types_and_strict_headers(
    site: TestClient, engine: AsyncEngine, settings: Settings, submitted_run: dict[str, Any]
) -> None:
    published = await _publish(engine, settings, str(submitted_run["id"]), "link")
    base = _path(published)

    page = site.get(base)
    script = site.get(f"{base}js/app.js")

    assert page.status_code == 200 and page.content == INDEX
    assert page.headers["content-type"] == "text/html; charset=utf-8"
    assert script.content == APP
    assert script.headers["content-type"] == "text/javascript; charset=utf-8"
    for response in (page, script):
        assert "connect-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "set-cookie" not in response.headers
    assert site.head(base).status_code == 200
    assert site.get(f"{base}missing.css").status_code == 404
    assert site.get(f"{base}../../etc/passwd").status_code == 404
    assert site.post(base).status_code == 405
    bare = site.get(base.rstrip("/"), follow_redirects=False)
    assert bare.status_code in (301, 307, 308) and bare.headers["location"].endswith(base)


async def test_a_members_site_asks_for_the_console_and_lets_a_ticket_in_once(
    site: TestClient, engine: AsyncEngine, settings: Settings, submitted_run: dict[str, Any]
) -> None:
    published = await _publish(engine, settings, str(submitted_run["id"]))
    base = _path(published)
    token = base.removeprefix("/s/").rstrip("/")

    door = site.get(base)
    assert door.status_code == 401
    assert f"{CONSOLE_URL}/api/v1/sites/open?t={token}" in door.text
    assert INDEX not in door.content

    ticket = _ticket(published.publication_id)
    entered = site.get(f"{base}__auth", params={"ticket": ticket}, follow_redirects=False)
    assert entered.status_code == 302 and entered.headers["location"] == base
    cookie = entered.headers["set-cookie"]
    assert f"Path={base}" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie

    assert site.get(base).content == INDEX
    replay = site.get(f"{base}__auth", params={"ticket": ticket}, follow_redirects=False)
    assert replay.status_code == 403


async def test_a_ticket_or_pass_for_one_site_opens_no_other(
    site: TestClient, engine: AsyncEngine, settings: Settings, submitted_run: dict[str, Any]
) -> None:
    run_id = str(submitted_run["id"])
    first = await _publish(engine, settings, run_id)
    second = await _publish(engine, settings, run_id)

    wrong = site.get(
        f"{_path(second)}__auth",
        params={"ticket": _ticket(first.publication_id)},
        follow_redirects=False,
    )
    assert wrong.status_code == 403
    expired = site.get(
        f"{_path(first)}__auth",
        params={"ticket": _ticket(first.publication_id, seconds=-1)},
        follow_redirects=False,
    )
    assert expired.status_code == 403

    entered = site.get(
        f"{_path(first)}__auth",
        params={"ticket": _ticket(first.publication_id)},
        follow_redirects=False,
    )
    pass_value = entered.cookies.get("site_pass")
    assert pass_value
    site.cookies.clear()
    borrowed = site.get(_path(second), cookies={"site_pass": pass_value})
    assert borrowed.status_code == 401


async def test_a_revoked_site_is_gone_even_with_a_valid_pass(
    site: TestClient, engine: AsyncEngine, settings: Settings, submitted_run: dict[str, Any]
) -> None:
    published = await _publish(engine, settings, str(submitted_run["id"]))
    base = _path(published)
    site.get(f"{base}__auth", params={"ticket": _ticket(published.publication_id)})
    assert site.get(base).status_code == 200

    async with engine.connect() as db:
        run_id = UUID(str(submitted_run["id"]))
        workspace = (
            await db.execute(select(RunRow.workspace_id).where(RunRow.id == run_id))
        ).scalar_one()
    await _publisher(engine, settings).revoke(
        workspace, published.publication_id, actor_id=None, request_id="t"
    )

    assert site.get(base).status_code == 404


def test_the_site_origin_must_not_be_the_console_s(settings: Settings) -> None:
    """Same origin, a site's script would ride the console's session."""
    with pytest.raises(ValueError, match="origin"):
        Settings.model_validate(
            {**settings.model_dump(), "site_public_url": CONSOLE_URL + "/sites"}
        )
