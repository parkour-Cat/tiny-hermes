"""The console's face of published sites (§1.26, v2.26): who may list, revoke,
extend, open to anyone, and get through the members' door."""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.infrastructure.tables import RunRow
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.shared.config import Settings
from tiny_hermes.sites.application.publisher import Published, SitePublisher, SiteSettings
from tiny_hermes.sites.domain.site import verify

from ..tenancy.test_workspace_members import _headers_for, _member, _seed_user
from ..workspace_file_support import seed_site
from .conftest import SITE_PUBLIC_URL, SITE_SECRET


async def _published(engine: AsyncEngine, settings: Settings, run_id: str) -> Published:
    await seed_site(engine, settings, run_id, {"site/index.html": b"<p>hi</p>"})
    async with engine.connect() as db:
        run = (await db.execute(select(RunRow).where(RunRow.id == UUID(run_id)))).one()
    publisher = SitePublisher(
        sessions=async_sessionmaker(engine, expire_on_commit=False),
        objects=MinioObjectStore(
            endpoint=settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            bucket=settings.s3_bucket,
        ),
        settings=SiteSettings(SITE_PUBLIC_URL, SITE_SECRET, 7),
    )
    return await publisher.publish(
        workspace_id=run.workspace_id,
        session_id=run.session_id,
        run_id=run.id,
        root="site",
        title="Hi",
    )


def _visibility(client: TestClient, site: str, headers: dict[str, str], value: str) -> int:
    return client.put(f"{site}/visibility", headers=headers, json={"visibility": value}).status_code


def _token(url: str) -> str:
    return url.removeprefix(f"{SITE_PUBLIC_URL}/s/").removesuffix("/")


async def test_members_list_their_sites_without_the_links(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    settings: Settings,
    submitted_run: dict[str, Any],
) -> None:
    """The link is shown once, where it was made. A list that repeated it would
    hand out working links to anyone who can read the list."""
    published = await _published(engine, settings, str(submitted_run["id"]))

    listed = client.get("/api/v1/sites", headers=scope)

    assert listed.status_code == 200, listed.text
    [site] = listed.json()
    assert site["id"] == str(published.publication_id)
    assert site["title"] == "Hi" and site["visibility"] == "members" and site["active"] is True
    assert site["run_id"] == str(submitted_run["id"])
    assert _token(published.url) not in listed.text


async def test_each_role_may_do_what_the_design_gives_it(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    settings: Settings,
    submitted_run: dict[str, Any],
) -> None:
    """Any member revokes; a developer or administrator extends; only an
    administrator opens a site to anyone with the link."""
    published = await _published(engine, settings, str(submitted_run["id"]))
    site = f"/api/v1/sites/{published.publication_id}"
    await _seed_user(engine, "Dev", "dev@example.com")
    await _seed_user(engine, "Vera", "vera@example.com")
    _member(client, scope, workspace_id, "dev@example.com", "developer")
    admin = _headers_for(client, "admin@example.com", workspace_id)
    viewer = _member(client, admin, workspace_id, "vera@example.com", "viewer")

    assert client.post(f"{site}/extend", headers=viewer).status_code == 403
    assert _visibility(client, site, viewer, "link") == 403

    developer = _headers_for(client, "dev@example.com", workspace_id)
    extended = client.post(f"{site}/extend", headers=developer)
    assert extended.status_code == 200, extended.text
    assert datetime.fromisoformat(extended.json()["expires_at"]) > datetime.now(UTC)
    assert _visibility(client, site, developer, "link") == 403

    admin = _headers_for(client, "admin@example.com", workspace_id)
    opened = client.put(f"{site}/visibility", headers=admin, json={"visibility": "link"})
    assert opened.status_code == 200, opened.text
    assert opened.json()["visibility"] == "link"
    assert _visibility(client, site, admin, "all") == 422

    viewer = _headers_for(client, "vera@example.com", workspace_id)
    revoked = client.post(f"{site}/revoke", headers=viewer)
    assert revoked.status_code == 204, revoked.text
    assert client.post(f"{site}/revoke", headers=viewer).status_code == 404


async def test_a_stranger_sees_no_sites_and_cannot_touch_one(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    settings: Settings,
    submitted_run: dict[str, Any],
) -> None:
    published = await _published(engine, settings, str(submitted_run["id"]))
    await _seed_user(engine, "Stan", "stan@example.com")
    stranger = _headers_for(client, "stan@example.com", workspace_id)

    assert client.get("/api/v1/sites", headers=stranger).status_code == 403
    assert (
        client.post(f"/api/v1/sites/{published.publication_id}/revoke", headers=stranger)
        .status_code
        == 403
    )


async def test_a_member_is_let_through_the_door_with_a_one_minute_ticket(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    settings: Settings,
    submitted_run: dict[str, Any],
) -> None:
    published = await _published(engine, settings, str(submitted_run["id"]))
    token = _token(published.url)

    opened = client.get("/api/v1/sites/open", params={"t": token}, follow_redirects=False)

    assert opened.status_code == 302, opened.text
    target = urlsplit(opened.headers["location"])
    assert f"{target.scheme}://{target.netloc}" == SITE_PUBLIC_URL
    assert target.path == f"/s/{token}/__auth"
    [ticket] = parse_qs(target.query)["ticket"]
    claims = verify(SITE_SECRET, "ticket", ticket, datetime.now(UTC))
    assert claims is not None and claims["p"] == str(published.publication_id)


async def test_a_stranger_is_turned_away_at_the_door(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    settings: Settings,
    submitted_run: dict[str, Any],
) -> None:
    published = await _published(engine, settings, str(submitted_run["id"]))
    await _seed_user(engine, "Stan", "stan@example.com")
    _headers_for(client, "stan@example.com", workspace_id)

    refused = client.get(
        "/api/v1/sites/open", params={"t": _token(published.url)}, follow_redirects=False
    )

    assert refused.status_code == 403


async def test_a_site_open_to_anyone_needs_no_ticket_and_a_revoked_one_opens_nothing(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    settings: Settings,
    submitted_run: dict[str, Any],
) -> None:
    published = await _published(engine, settings, str(submitted_run["id"]))
    site = f"/api/v1/sites/{published.publication_id}"
    token = _token(published.url)
    client.put(f"{site}/visibility", headers=scope, json={"visibility": "link"})

    opened = client.get("/api/v1/sites/open", params={"t": token}, follow_redirects=False)
    assert opened.status_code == 302
    assert opened.headers["location"] == published.url

    client.post(f"{site}/revoke", headers=scope)
    gone = client.get("/api/v1/sites/open", params={"t": token}, follow_redirects=False)
    assert gone.status_code == 404
