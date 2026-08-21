"""Task-9 review finding C: the audit trail said `actor_type="user"` no
matter who actually acted.

`memory/infrastructure/sql_subject_store.py::append_audit` and
`runs/infrastructure/sql_approval_store.py::append_audit` hardcoded
`actor_type="user"` with a caller-supplied `actor_id`. For an end user
erasing their own data, that `actor_id` is a key into `end_users`, not
`users` — an auditor joining to `users` finds nothing, or worse, collides
with an unrelated workspace member. It also erased the one distinction
§4.6 exists to draw: 代办 (an admin acting for the subject, audited as such)
versus 本人 (the subject acting for themselves) — both used to record as
`user` regardless of which subject the `本人` even was.

This suite proves the fix along both axes reachable today: an end user's
own self-service erase now says `end_user`; a console member's own
self-service erase, and a steward acting on a *fellow* console member's
behalf, both still say `user` — the two are distinguishable by more than
which route was hit.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.api.app import create_app
from tiny_hermes.identity.presentation.end_user_dependencies import END_USER_SESSION_COOKIE
from tiny_hermes.shared.config import Settings

from ..conftest import VALID_SPEC

ISSUER = "https://idp.acme.example"
CHANNEL = "web"
ALIAS = "actor-type-bot"
PASSWORD = "long-pass-123"  # noqa: S105 - fixed local test credential


@pytest.fixture
def client(empty_database: None, settings: Settings) -> Iterator[TestClient]:
    """Same override every end-user suite makes: the end-user session
    cookie is `Secure` unconditionally, and `httpx`'s jar only attaches a
    `Secure` cookie to an `https://` base URL."""
    del empty_database
    with TestClient(create_app(settings=settings), base_url="https://testserver") as value:
        yield value


def _rsa_keypair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


RSA_PRIVATE_PEM, RSA_PUBLIC_PEM = _rsa_keypair()


def _credential(*, workspace_id: str, sub: str, agents: list[str] | None = None) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "sub": sub,
        "aud": workspace_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "agents": agents if agents is not None else [ALIAS],
    }
    return jwt.encode(payload, RSA_PRIVATE_PEM, algorithm="RS256")


async def _seed_user(engine: AsyncEngine, display_name: str, subject: str) -> None:
    """A login-able user with a known password, the same trick
    `test_end_user_session_audit.py::_invite_and_login_developer` uses:
    inviting a member never sets a password on its own, so a steward
    needs one seeded directly before the invite can turn into a login."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (id, status, display_name, is_platform_admin, created_at) "
                "VALUES (gen_random_uuid(), 'active', :name, false, now())"
            ),
            {"name": display_name},
        )
        await connection.execute(
            text(
                "INSERT INTO auth_identities "
                "(id, user_id, provider, subject, password_hash, created_at) "
                "SELECT gen_random_uuid(), id, 'local', :subject, "
                "  (SELECT password_hash FROM auth_identities LIMIT 1), now() "
                "FROM users WHERE display_name = :name"
            ),
            {"subject": subject, "name": display_name},
        )


async def _audit_rows(engine: AsyncEngine, action: str) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT actor_type, actor_id, resource_id FROM audit_events "
                "WHERE action = :a ORDER BY created_at"
            ),
            {"a": action},
        )
        return [dict(row) for row in rows.mappings()]


async def test_an_end_users_own_erasure_is_audited_as_end_user(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
) -> None:
    client.post(
        "/api/v1/channel-issuers",
        headers=scope,
        json={
            "channel": CHANNEL,
            "issuer": ISSUER,
            "public_key": RSA_PUBLIC_PEM,
            "allowed_origins": ["https://acme.example"],
        },
    )
    spec = {**VALID_SPEC, "end_user_access": {"enabled": True}}
    created = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Actor Type Bot", "alias": ALIAS}
    )
    agent_id = str(created.json()["id"])
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": spec},
    )
    client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )

    exchanged = client.post(
        "/api/v1/end-user/sessions",
        headers={
            "Authorization": f"Bearer {_credential(workspace_id=workspace_id, sub='zhang')}",
            "X-Workspace-Id": workspace_id,
        },
    )
    assert exchanged.status_code == 201, exchanged.text
    assert exchanged.cookies.get(END_USER_SESSION_COOKIE) is not None
    end_user_id = UUID(exchanged.json()["end_user_id"])

    session_created = client.post(f"/api/v1/end-user/agents/{ALIAS}/sessions", json={})
    assert session_created.status_code == 201, session_created.text
    run_created = client.post(
        f"/api/v1/end-user/sessions/{session_created.json()['id']}/runs",
        headers={"Idempotency-Key": "actor-type-1"},
        json={"input": "hello"},
    )
    assert run_created.status_code == 201, run_created.text

    erased = client.post("/api/v1/end-user/subjects/me/erase")
    assert erased.status_code == 200, erased.text

    rows = await _audit_rows(engine, "subject.erased")
    assert rows
    row = rows[-1]
    assert row["actor_type"] == "end_user"
    assert row["actor_id"] == end_user_id
    assert row["resource_id"] == end_user_id


async def test_a_console_members_own_self_service_erasure_still_says_user(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
) -> None:
    """Control: the fix must not turn a genuine console self-service erasure
    into anything but `user` — the actor really is a `users.id` here."""
    agent_id = agent_with_scenario("remember_once", alias="console-eraser")
    session_id = session_for(agent_id)
    run = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "actor-type-2"},
        json={"session_id": session_id, "input": "remember this"},
    )
    assert run.status_code == 201, run.text

    async with engine.connect() as connection:
        found = await connection.execute(
            text("SELECT caller_id FROM sessions WHERE id = :s"), {"s": UUID(session_id)}
        )
        subject_id = found.scalar()

    erased = client.post(f"/api/v1/subjects/{subject_id}/erase", headers=scope)
    assert erased.status_code == 200, erased.text

    rows = await _audit_rows(engine, "subject.erased")
    assert rows
    row = rows[-1]
    assert row["actor_type"] == "user"
    assert row["actor_id"] == subject_id


async def test_a_steward_erasing_a_fellow_members_data_is_audited_as_user_on_both_lines(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
) -> None:
    """代办: a workspace admin acting on somebody else's own data — the
    bootstrap admin's, here, since they are a known `users.id` already
    sitting in `scope`'s own workspace. Both the "who authorized this" line
    (`subject.acted_on_behalf`) and the action's own line (`subject.erased`)
    name the steward, a real `users.id`, so both stay `user` — distinguishable
    from the end user's own erasure above by `actor_id`, not by a
    coincidence of type."""
    async with engine.connect() as connection:
        original_admin_id = await connection.scalar(
            text("SELECT id FROM users WHERE display_name = 'Admin'")
        )
    assert original_admin_id is not None

    await _seed_user(engine, "Steward", "steward@example.com")
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": "steward@example.com", "role": "workspace_admin"},
    )
    assert invited.status_code == 201, invited.text

    # Same `client`, same cookie jar, same trick
    # `test_end_user_session_audit.py::_invite_and_login_developer` uses:
    # logging in again replaces the admin's session cookie with the
    # steward's, so every call after this one is the steward's, not the
    # original admin's. A second `TestClient` wrapping the same app was
    # tried here first and does not work — its own httpx transport ends up
    # on a different anyio task than the one the app's DB engine was
    # created on, and asyncpg refuses to hand a connection across that
    # boundary.
    login = client.post(
        "/api/v1/auth/sessions",
        json={"subject": "steward@example.com", "password": PASSWORD},
    )
    assert login.status_code == 201, login.text
    steward_headers = {
        "X-Workspace-Id": workspace_id,
        "X-CSRF-Token": login.cookies["tiny_hermes_csrf"],
    }

    erased = client.post(f"/api/v1/subjects/{original_admin_id}/erase", headers=steward_headers)
    assert erased.status_code == 200, erased.text

    behalf_rows = await _audit_rows(engine, "subject.acted_on_behalf")
    assert behalf_rows
    assert behalf_rows[-1]["actor_type"] == "user"
    assert behalf_rows[-1]["resource_id"] == original_admin_id

    erased_rows = await _audit_rows(engine, "subject.erased")
    assert erased_rows
    assert erased_rows[-1]["actor_type"] == "user"
    assert erased_rows[-1]["resource_id"] == original_admin_id


async def test_erase_data_written_by_the_console_and_by_an_end_user_are_never_confused(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    agent_with_scenario: Callable[..., str],
    session_for: Callable[[str], str],
) -> None:
    """The distinction the fix restores, seen together: two erasures, one
    `end_user`, one `user`, and nothing about reading the audit table alone
    would let the two be mistaken for each other."""
    agent_id = agent_with_scenario("remember_once", alias="console-eraser-2")
    session_id = session_for(agent_id)
    client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "actor-type-3"},
        json={"session_id": session_id, "input": "remember this too"},
    )
    async with engine.connect() as connection:
        found = await connection.execute(
            text("SELECT caller_id FROM sessions WHERE id = :s"), {"s": UUID(session_id)}
        )
        console_subject_id = found.scalar()
    console_erased = client.post(f"/api/v1/subjects/{console_subject_id}/erase", headers=scope)
    assert console_erased.status_code == 200, console_erased.text

    client.post(
        "/api/v1/channel-issuers",
        headers=scope,
        json={
            "channel": CHANNEL,
            "issuer": ISSUER,
            "public_key": RSA_PUBLIC_PEM,
            "allowed_origins": ["https://acme.example"],
        },
    )
    spec = {**VALID_SPEC, "end_user_access": {"enabled": True}}
    other_alias = "actor-type-bot-2"
    created = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Actor Type Bot 2", "alias": other_alias}
    )
    other_agent_id = str(created.json()["id"])
    draft = client.put(
        f"/api/v1/agents/{other_agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": spec},
    )
    client.post(
        f"/api/v1/agents/{other_agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    exchanged = client.post(
        "/api/v1/end-user/sessions",
        headers={
            "Authorization": (
                f"Bearer {_credential(workspace_id=workspace_id, sub='li', agents=[other_alias])}"
            ),
            "X-Workspace-Id": workspace_id,
        },
    )
    assert exchanged.status_code == 201, exchanged.text
    end_user_id = UUID(exchanged.json()["end_user_id"])
    session_created = client.post(f"/api/v1/end-user/agents/{other_alias}/sessions", json={})
    assert session_created.status_code == 201, session_created.text
    client.post(
        f"/api/v1/end-user/sessions/{session_created.json()['id']}/runs",
        headers={"Idempotency-Key": "actor-type-4"},
        json={"input": "hello again"},
    )
    end_user_erased = client.post("/api/v1/end-user/subjects/me/erase")
    assert end_user_erased.status_code == 200, end_user_erased.text

    rows = await _audit_rows(engine, "subject.erased")
    by_resource = {row["resource_id"]: row["actor_type"] for row in rows}
    assert by_resource[console_subject_id] == "user"
    assert by_resource[end_user_id] == "end_user"
