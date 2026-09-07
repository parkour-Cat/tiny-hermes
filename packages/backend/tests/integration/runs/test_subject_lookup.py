"""Finding the subject a data-rights request is about.

§4.6 lets a workspace administrator act on a subject's behalf — export,
correct, forget, erase — and every one of those routes takes the subject's
**internal** id. Nothing gave an administrator that id: `end_users.id` is a
uuid this platform minted, and a request arrives naming a person the way
the enterprise's own directory names them.

So the four routes were reachable only by reading the database by hand,
which is the same as not being reachable. This is the door in.

Deliberately a lookup rather than a listing. "Who are all the end users in
this workspace" is a different question with a different disclosure, and
§4.6 does not grant it; answering one named person is what acting on a
request requires.
"""

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def _seed_subject(
    engine: AsyncEngine,
    *,
    workspace_id: str,
    channel: str = "web",
    external_user_id: str = "alice@example.com",
) -> UUID:
    subject = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO end_users (id, workspace_id, created_at) VALUES (:i, :w, now())"),
            {"i": subject, "w": UUID(workspace_id)},
        )
        await connection.execute(
            text(
                "INSERT INTO external_identities "
                "(id, workspace_id, channel, external_user_id, end_user_id, created_at) "
                "VALUES (gen_random_uuid(), :w, :c, :e, :i, now())"
            ),
            {"w": UUID(workspace_id), "c": channel, "e": external_user_id, "i": subject},
        )
    return subject


async def test_session_lookup_requires_a_scoped_end_user_and_is_audited(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    workspace_id: str,
    session_id: str,
) -> None:
    path = f"/api/v1/subjects/from-session/{session_id}"
    assert client.get(path, headers=scope).status_code == 404
    subject = await _seed_subject(engine, workspace_id=workspace_id)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE sessions SET caller_type='end_user', caller_id=:subject WHERE id=:session"
            ),
            {"subject": subject, "session": UUID(session_id)},
        )
    response = client.get(path, headers=scope)
    assert response.status_code == 200, response.text
    assert response.json()["subject_id"] == str(subject)
    assert client.get(path, headers={**scope, "X-Workspace-Id": str(uuid4())}).status_code == 404
    async with engine.connect() as connection:
        count = await connection.scalar(
            text(
                "SELECT count(*) FROM audit_events "
                "WHERE action='subject.resolved_from_session' AND resource_id=:subject"
            ),
            {"subject": subject},
        )
    assert count == 1


async def test_a_steward_finds_the_subject_a_request_names(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, workspace_id: str
) -> None:
    subject = await _seed_subject(engine, workspace_id=workspace_id)

    found = client.get(
        "/api/v1/subjects/lookup",
        headers=scope,
        params={"channel": "web", "external_user_id": "alice@example.com"},
    )

    assert found.status_code == 200, found.text
    assert found.json()["subject_id"] == str(subject)
    # And the id is one the routes that act on it actually accept.
    exported = client.get(f"/api/v1/subjects/{subject}/export", headers=scope)
    assert exported.status_code == 200, exported.text


async def test_a_name_nobody_here_uses_is_a_404_that_says_nothing_else(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, workspace_id: str
) -> None:
    await _seed_subject(engine, workspace_id=workspace_id)

    missing = client.get(
        "/api/v1/subjects/lookup",
        headers=scope,
        params={"channel": "web", "external_user_id": "nobody@example.com"},
    )

    assert missing.status_code == 404, missing.text
    # Not echoed back: this endpoint answers whether a named person is known
    # here, and an error page repeating the name puts it in logs and in a
    # browser's history for a person who turned out not to exist.
    assert "nobody@example.com" not in missing.text


async def test_the_same_name_in_another_workspace_is_not_found(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, workspace_id: str
) -> None:
    """§23 assertion 1. `external_user_id` is unique per workspace and
    channel, so the same directory name exists in two tenants — and one
    tenant's administrator must not resolve the other's subject."""
    await _seed_subject(engine, workspace_id=workspace_id)
    elsewhere = {**scope, "X-Workspace-Id": str(uuid4())}

    found = client.get(
        "/api/v1/subjects/lookup",
        headers=elsewhere,
        params={"channel": "web", "external_user_id": "alice@example.com"},
    )

    assert found.status_code in (403, 404), found.text
    # And the same call from the owning workspace resolves — without this the
    # test passes just as well against a route that does not exist.
    assert (
        client.get(
            "/api/v1/subjects/lookup",
            headers=scope,
            params={"channel": "web", "external_user_id": "alice@example.com"},
        ).status_code
        == 200
    )


async def test_an_erased_subject_still_resolves_and_says_so(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, workspace_id: str
) -> None:
    """§344: the row survives its own erasure because Runs reference it.

    An administrator handling a second request from the same person needs
    "already erased, on this date" rather than "no such person" — those are
    different answers and only one of them is true.
    """
    subject = await _seed_subject(engine, workspace_id=workspace_id)
    erased = client.post(f"/api/v1/subjects/{subject}/erase", headers=scope)
    assert erased.status_code == 200, erased.text

    found = client.get(
        "/api/v1/subjects/lookup",
        headers=scope,
        params={"channel": "web", "external_user_id": "alice@example.com"},
    )

    assert found.status_code == 200, found.text
    assert found.json()["erased_at"] is not None


async def test_a_viewer_cannot_resolve_anybody(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    workspace_id: str,
) -> None:
    """§4.6 gives this row to a steward. A viewer learning that a named
    person is an end user of this workspace is a disclosure on its own,
    before any of their data is touched."""
    await _seed_subject(engine, workspace_id=workspace_id)
    async with engine.begin() as connection:
        user_id = uuid4()
        await connection.execute(
            text(
                "INSERT INTO users (id, status, display_name, is_platform_admin, "
                "created_at) VALUES (:id, 'active', 'Vera', false, now())"
            ),
            {"id": user_id},
        )
        await connection.execute(
            text(
                "INSERT INTO auth_identities "
                "(id, user_id, provider, subject, password_hash, created_at) "
                "SELECT gen_random_uuid(), :id, 'local', 'vera@example.com', "
                "  (SELECT password_hash FROM auth_identities LIMIT 1), now()"
            ),
            {"id": user_id},
        )
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": "vera@example.com", "role": "viewer"},
    )
    assert invited.status_code == 201, invited.text
    login = client.post(
        "/api/v1/auth/sessions",
        json={"subject": "vera@example.com", "password": "long-pass-123"},
    )
    assert login.status_code == 201, login.text
    viewer: dict[str, Any] = {
        "X-CSRF-Token": login.cookies["tiny_hermes_csrf"],
        "X-Workspace-Id": workspace_id,
    }

    assert client.get(f"/api/v1/subjects/from-session/{uuid4()}", headers=viewer).status_code == 403
    refused = client.get(
        "/api/v1/subjects/lookup",
        headers=viewer,
        params={"channel": "web", "external_user_id": "alice@example.com"},
    )

    assert refused.status_code == 403, refused.text


async def test_subject_search_is_bounded_audited_and_workspace_scoped(
    client: TestClient,
    scope: dict[str, str],
    engine: AsyncEngine,
    workspace_id: str,
) -> None:
    subject = await _seed_subject(engine, workspace_id=workspace_id)
    await _seed_subject(engine, workspace_id=workspace_id, external_user_id="alice-two@example.com")
    response = client.get(
        "/api/v1/subjects/search",
        headers=scope,
        params={"q": "alice", "channel": "web", "limit": 1},
    )
    assert response.status_code == 200, response.text
    assert response.json()["has_more"] is True
    assert len(response.json()["items"]) == 1
    exact = client.get(
        "/api/v1/subjects/search",
        headers=scope,
        params={"q": "alice@example.com", "channel": "web"},
    )
    assert exact.json()["items"][0]["subject_id"] == str(subject)
    assert (
        client.get("/api/v1/subjects/search", headers=scope, params={"q": "a"}).status_code == 422
    )
    elsewhere = {**scope, "X-Workspace-Id": str(uuid4())}
    assert (
        client.get("/api/v1/subjects/search", headers=elsewhere, params={"q": "alice"}).json()[
            "items"
        ]
        == []
    )
    async with engine.begin() as db:
        assert (
            await db.scalar(
                text("SELECT count(*) FROM audit_events WHERE action='subject.searched'")
            )
            >= 2
        )
        await db.execute(text("UPDATE users SET is_platform_admin=false"))
        await db.execute(text("UPDATE memberships SET role='developer'"))
    assert (
        client.get("/api/v1/subjects/search", headers=scope, params={"q": "alice"}).status_code
        == 403
    )
