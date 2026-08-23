"""§4, exercised as real HTTP: `GET /api/v1/audit-events` for four of §4.6's
five subjects, and a firm 403 for the fifth.

Each test seeds rows directly with SQL (the same trick
`test_end_user_session_audit.py` uses for memories) so a row's `actor_id`,
`resource_id` and `context` are exactly what the assertion needs, rather
than depending on which actions some other route happens to emit today.
"""

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.audit.domain import query as query_module
from tiny_hermes.audit.domain.query import MAX_PAGE_SIZE

from ..conftest import PASSWORD

CROSS_WORKSPACE_READ = "audit.cross_workspace_read"


async def _seed_audit_row(
    engine: AsyncEngine,
    *,
    workspace_id: str,
    actor_id: UUID,
    action: str,
    resource_type: str = "agent",
    resource_id: UUID | None = None,
    context: dict[str, Any] | None = None,
) -> UUID:
    row_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO audit_events (id, workspace_id, actor_type, actor_id, "
                "action, resource_type, resource_id, result, request_id, context, "
                "created_at) VALUES (:id, :workspace_id, 'user', :actor_id, :action, "
                ":resource_type, :resource_id, 'succeeded', :request_id, "
                "CAST(:context AS JSON), now())"
            ),
            {
                "id": row_id,
                "workspace_id": UUID(workspace_id),
                "actor_id": actor_id,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id or uuid4(),
                "request_id": f"seed-{row_id}",
                "context": json.dumps(context or {}),
            },
        )
    return row_id


async def _seed_user(
    engine: AsyncEngine, display_name: str, subject: str, *, is_platform_admin: bool = False
) -> UUID:
    user_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (id, status, display_name, is_platform_admin, created_at) "
                "VALUES (:id, 'active', :name, :platform, now())"
            ),
            {"id": user_id, "name": display_name, "platform": is_platform_admin},
        )
        await connection.execute(
            text(
                "INSERT INTO auth_identities "
                "(id, user_id, provider, subject, password_hash, created_at) "
                "SELECT gen_random_uuid(), :id, 'local', :subject, "
                "  (SELECT password_hash FROM auth_identities LIMIT 1), now()"
            ),
            {"id": user_id, "subject": subject},
        )
    return user_id


def _login(client: TestClient, subject: str) -> dict[str, str]:
    login = client.post(
        "/api/v1/auth/sessions", json={"subject": subject, "password": PASSWORD}
    )
    assert login.status_code == 201, login.text
    return {"X-CSRF-Token": login.cookies["tiny_hermes_csrf"]}


async def _audit_rows(engine: AsyncEngine, action: str) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT workspace_id, actor_id, resource_id, context FROM audit_events "
                "WHERE action = :a"
            ),
            {"a": action},
        )
        return [dict(row) for row in rows.mappings()]


async def test_workspace_admin_reads_full_trail_of_their_own_workspace(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    """The bootstrap admin created this workspace, which makes them its
    `WORKSPACE_ADMIN` member (`WorkspaceService.create_workspace`) — §4.6's
    本空间只读, not the platform-admin cross-workspace branch."""
    listed = client.get("/api/v1/audit-events", headers=scope)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert any(item["action"] == "workspace.created" for item in body["items"])

    assert await _audit_rows(engine, CROSS_WORKSPACE_READ) == []


async def test_platform_admin_with_no_membership_reads_cross_workspace_and_it_is_logged(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    other_admin_id = await _seed_user(
        engine, "Other Admin", "other-admin@example.com", is_platform_admin=True
    )
    headers = {**_login(client, "other-admin@example.com"), "X-Workspace-Id": workspace_id}

    listed = client.get("/api/v1/audit-events", headers=headers)
    assert listed.status_code == 200, listed.text
    assert any(item["action"] == "workspace.created" for item in listed.json()["items"])

    logged = await _audit_rows(engine, CROSS_WORKSPACE_READ)
    assert len(logged) == 1
    assert logged[0]["actor_id"] == other_admin_id
    assert logged[0]["resource_id"] == UUID(workspace_id)
    assert logged[0]["workspace_id"] == UUID(workspace_id)


async def test_developer_sees_own_actions_and_resources_they_touched_only(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    developer_id = await _seed_user(engine, "Dev", "dev@example.com")
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": "dev@example.com", "role": "developer"},
    )
    assert invited.status_code == 201, invited.text

    touched_resource = uuid4()
    own_row = await _seed_audit_row(
        engine,
        workspace_id=workspace_id,
        actor_id=developer_id,
        action="agent.published",
        resource_id=touched_resource,
    )
    # Somebody else acted on the *same* resource afterwards — still visible.
    followed_up = await _seed_audit_row(
        engine,
        workspace_id=workspace_id,
        actor_id=uuid4(),
        action="agent.rolled_back",
        resource_id=touched_resource,
    )
    # A resource this developer never touched at all.
    unrelated = await _seed_audit_row(
        engine, workspace_id=workspace_id, actor_id=uuid4(), action="agent.published"
    )

    headers = {**_login(client, "dev@example.com"), "X-Workspace-Id": workspace_id}
    listed = client.get("/api/v1/audit-events", headers=headers)
    assert listed.status_code == 200, listed.text
    ids = {UUID(item["id"]) for item in listed.json()["items"]}

    assert own_row in ids
    assert followed_up in ids
    assert unrelated not in ids


async def test_viewer_context_is_stripped_of_every_unregistered_key(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    await _seed_user(engine, "View", "view@example.com")
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": "view@example.com", "role": "viewer"},
    )
    assert invited.status_code == 201, invited.text

    row_id = await _seed_audit_row(
        engine,
        workspace_id=workspace_id,
        actor_id=uuid4(),
        action="run.paused",
        resource_type="run",
        context={"session_summary": "the customer is upset about pricing"},
    )

    headers = {**_login(client, "view@example.com"), "X-Workspace-Id": workspace_id}
    listed = client.get("/api/v1/audit-events", headers=headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    item = next(entry for entry in body["items"] if entry["id"] == str(row_id))

    assert item["context"] == {}
    assert "session_summary" not in str(body)
    assert "the customer is upset" not in str(body)


async def test_end_user_cookie_gets_403_with_no_exceptions(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    """§4.6: 否. `_CONSOLE_ONLY` refuses this before the route's own code
    runs — proven here rather than assumed, matching every other §4.6 "否"
    cell this codebase has pinned with its own test.

    The console session cookie the `client`/`scope` fixtures already left
    in the jar has to go first: `reject_end_user_caller`'s own finding-G
    fix (`identity/presentation/end_user_dependencies.py`) only refuses a
    request that carries *no* console credential alongside the end-user
    cookie, so a jar holding both would pass this check on the console
    session alone and never exercise the refusal this test is for.
    """
    del scope
    await _seed_user(engine, "irrelevant", "irrelevant@example.com")
    client.cookies.clear()
    client.cookies.set("tiny_hermes_end_user_session", "whatever-a-cookie-looks-like")

    listed = client.get(
        "/api/v1/audit-events", headers={"X-Workspace-Id": workspace_id}
    )
    assert listed.status_code == 403, listed.text


async def test_a_stranger_with_no_membership_and_no_platform_role_is_refused(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    await _seed_user(engine, "Stranger", "stranger@example.com")
    headers = {**_login(client, "stranger@example.com"), "X-Workspace-Id": workspace_id}

    listed = client.get("/api/v1/audit-events", headers=headers)
    assert listed.status_code == 403, listed.text


async def test_filters_and_pagination_are_honoured(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    for _ in range(3):
        await _seed_audit_row(
            engine, workspace_id=workspace_id, actor_id=uuid4(), action="probe.tick"
        )

    first = client.get(
        "/api/v1/audit-events",
        headers=scope,
        params={"action": "probe.tick", "limit": 2},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert all(item["action"] == "probe.tick" for item in body["items"])

    second = client.get(
        "/api/v1/audit-events",
        headers=scope,
        params={"action": "probe.tick", "limit": 2, "offset": 2},
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["items"]) == 1
    assert second.json()["has_more"] is False


async def test_a_limit_above_the_ceiling_is_clamped_rather_than_refused(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    """The HTTP half of `test_limit_is_clamped_not_refused`.

    `filter_for` clamps an oversized limit instead of rejecting it, but a
    `Query(le=MAX_PAGE_SIZE)` on the route would have turned exactly those
    requests into a 422 before the domain rule ever ran — the same shape as
    a response model that drops fields the service returns: the rule is
    right, tested, and unreachable. So this asserts the boundary, not the
    function that sits behind it.
    """
    await _seed_audit_row(
        engine,
        workspace_id=workspace_id,
        actor_id=uuid4(),
        action="agent.published",
        resource_type="agent",
        context={},
    )

    listed = client.get("/api/v1/audit-events?limit=100000", headers=scope)

    assert listed.status_code == 200, listed.text
    assert len(listed.json()["items"]) <= MAX_PAGE_SIZE


async def test_a_redacted_page_says_so_rather_than_looking_like_a_full_one(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    """§5: the reader has to be able to tell that something was removed.

    A redacted `context` comes back `{}` — and so does a row that never had
    a `context` to begin with. Without the page saying which it is, the two
    are the same bytes, and a viewer reading an incident trail cannot tell
    "nothing was recorded here" from "you are not allowed to see what was
    recorded here". Those lead to opposite conclusions, and the second one
    is the one that makes somebody close an investigation early.
    """
    await _seed_user(engine, "View2", "view2@example.com")
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": "view2@example.com", "role": "viewer"},
    )
    assert invited.status_code == 201, invited.text
    row_id = await _seed_audit_row(
        engine,
        workspace_id=workspace_id,
        actor_id=uuid4(),
        action="run.paused",
        resource_type="run",
        context={"reason": "held for review"},
    )

    # The admin's read goes first, deliberately: `_login` puts the viewer's
    # session cookie into this client's *shared* jar, and a cookie outranks
    # the `scope` headers — the same trap `_invite_and_login_developer`
    # documents in this file. Asking as the admin afterwards would silently
    # be asking as the viewer again, and the test would agree with itself.
    admin = client.get("/api/v1/audit-events", headers=scope).json()
    headers = {**_login(client, "view2@example.com"), "X-Workspace-Id": workspace_id}
    viewer = client.get("/api/v1/audit-events", headers=headers).json()

    assert admin["visibility"] == "full"
    assert viewer["visibility"] == "redacted"
    # And the thing the field exists to distinguish: same row, both readers,
    # one of them told that something was taken out.
    #
    # Found by id, not by position. Inviting the viewer above is itself an
    # audited action, so this workspace has more than one row by now, and
    # `created_at DESC` has no tiebreaker — two rows written in the same
    # instant come back in whatever order the plan happens to produce. This
    # test passed alone and failed in the full suite for exactly that reason.
    def find(page: dict[str, Any]) -> dict[str, Any]:
        return next(entry for entry in page["items"] if entry["id"] == str(row_id))

    assert find(admin)["context"] == {"reason": "held for review"}
    assert find(viewer)["context"] == {}


async def test_an_export_carries_the_same_scope_the_reader_already_had(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    """§26's "审计查询与导出" — and the half that matters is the second word.

    An export is a second door onto the same rows, so the thing worth
    testing is not that it produces a file: it is that the file is narrowed
    exactly as the reader's own page would have been. A viewer's export with
    `context` intact would hand them, in a spreadsheet, the detail the API
    refuses them on screen — and nobody would notice, because the export
    "worked".
    """
    await _seed_user(engine, "Exp", "exp@example.com")
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": "exp@example.com", "role": "viewer"},
    )
    assert invited.status_code == 201, invited.text
    await _seed_audit_row(
        engine,
        workspace_id=workspace_id,
        actor_id=uuid4(),
        action="run.paused",
        resource_type="run",
        context={"reason": "a detail a viewer may not read"},
    )

    admin_export = client.get("/api/v1/audit-events/export", headers=scope)
    headers = {**_login(client, "exp@example.com"), "X-Workspace-Id": workspace_id}
    viewer_export = client.get("/api/v1/audit-events/export", headers=headers)

    assert admin_export.status_code == 200, admin_export.text
    assert viewer_export.status_code == 200, viewer_export.text
    # A file, not a JSON page: an auditor opens this in a spreadsheet.
    assert admin_export.headers["content-type"].startswith("text/csv")
    assert "attachment" in admin_export.headers.get("content-disposition", "")
    # The row reaches both — redaction removes a column, not a row (§2).
    assert "run.paused" in admin_export.text
    assert "run.paused" in viewer_export.text
    # And the detail reaches only the reader whose scope allows it.
    assert "a detail a viewer may not read" in admin_export.text
    assert "a detail a viewer may not read" not in viewer_export.text


async def test_an_end_user_cookie_cannot_export_either(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    """§4.6 gives an end user 否 on audit, and a second door must be shut the
    same way. A guard applied to the list route and forgotten on the export
    is the shape this repository keeps finding.

    The request carries the end-user cookie **and no console credential**,
    which is what `reject_end_user_caller` refuses since task-9 finding G —
    presence alongside a valid console session is a member who also happens
    to be an end user, and locking them out was the bug that finding fixed.
    An earlier version of this test sent the admin's CSRF too and passed
    with a 200; it was asserting behaviour I had deliberately removed.
    """
    del engine, workspace_id
    fresh = TestClient(client.app)
    fresh.cookies.set("tiny_hermes_end_user_session", "not-a-real-session")

    refused = fresh.get("/api/v1/audit-events/export", headers={"X-Workspace-Id": str(uuid4())})

    assert refused.status_code == 403, refused.text


async def test_an_export_is_not_silently_cut_off_at_one_page(
    client: TestClient, scope: dict[str, str], workspace_id: str, engine: AsyncEngine
) -> None:
    """An export that stops at `MAX_PAGE_SIZE` and says nothing is the worst
    of the shapes this repository keeps finding: the file opens, the columns
    are right, every row in it is true — and the ones that would have
    mattered are simply absent. An auditor draws conclusions from it.

    The first version of this route passed `MAX_EXPORT_ROWS` to `filter_for`,
    which clamps to `MAX_PAGE_SIZE`; it returned 200 rows out of any number.
    Every other test in this file passed, because none of them seeds a
    second page.
    """
    marks = [uuid4() for _ in range(MAX_PAGE_SIZE + 5)]
    for mark in marks:
        await _seed_audit_row(
            engine,
            workspace_id=workspace_id,
            actor_id=uuid4(),
            action="run.started",
            resource_id=mark,
        )

    exported = client.get("/api/v1/audit-events/export", headers=scope)

    assert exported.status_code == 200, exported.text
    body = exported.text
    missing = [str(mark) for mark in marks if str(mark) not in body]
    assert not missing, f"{len(missing)} of {len(marks)} rows never reached the file"


async def test_an_export_over_the_ceiling_refuses_instead_of_truncating(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling's whole purpose is the refusal, and at 50,000 rows no
    test would ever reach it — so the branch that refuses would be the one
    piece of this route nobody had run. The ceiling is read from the module
    at call time; lowering it here exercises the real path.
    """
    monkeypatch.setattr(query_module, "MAX_EXPORT_ROWS", 2)
    for _ in range(3):
        await _seed_audit_row(
            engine, workspace_id=workspace_id, actor_id=uuid4(), action="run.started"
        )

    refused = client.get("/api/v1/audit-events/export", headers=scope)

    assert refused.status_code == 413, refused.text
    # Says what to do about it — a bare "too large" leaves an auditor with a
    # filter they cannot fix.
    assert "Narrow" in refused.json()["detail"]
