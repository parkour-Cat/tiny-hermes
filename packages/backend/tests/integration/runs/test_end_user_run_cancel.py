"""§10's Run half: the other row §4.6's matrix left unreachable.

`Run 暂停、继续与取消 | 终端用户 | 本人` had no route at all — the console's
own `/api/v1/runs/{id}/{pause,resume,cancel}` are `_CONSOLE_ONLY` and
`reject_end_user_caller` refuses an end-user cookie there before anything
else is asked. A Run that stopped — waiting on its own confirmation, paused,
or simply still queued behind another — had no door its own end user could
reach at all, which is what kept it stuck exactly as long as an unanswered
`user_confirmation` did (see `test_end_user_approvals.py`'s own docstring).

Only cancel. §10 is explicit that pause and resume are left out on purpose —
the chat surface `apps/chat-web` builds has no place in its UI for "pause",
and cancel is the one action erasure and "I don't want this any more" both
need. The last two tests below pin that restraint down as a fact about the
routes rather than leave it as a sentence in a plan: `pause`/`resume` are not
partially built and gated off, they simply do not exist under
`/api/v1/end-user/runs/`.

`cancel_end_user_run` (`runs/application/service.py`) reuses
`RunCoordination.control_run`'s own path — `ControlRunCommand` through
`SqlRunStore.control_run` into `RunStateMachine` — the same seam the
console's `/cancel` uses. The only new work is the ownership check
(`get_end_user_run`, already exercised by `test_end_user_reads.py`) standing
in for `_require_role`, because an end user is never a workspace member and
has no Role for that check to read.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
ALIAS = "support-bot"


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


def _credential(*, workspace_id: str, sub: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "sub": sub,
        "aud": workspace_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "agents": [ALIAS],
    }
    return jwt.encode(payload, RSA_PRIVATE_PEM, algorithm="RS256")


@pytest.fixture
def client(empty_database: None, settings: Settings) -> Iterator[TestClient]:
    """Same override every end-user suite makes: the session cookie is
    `Secure` unconditionally, and `httpx`'s jar only attaches a `Secure`
    cookie to an `https://` base URL."""
    del empty_database
    with TestClient(create_app(settings=settings), base_url="https://testserver") as value:
        yield value


@pytest.fixture
def registered_issuer(client: TestClient, scope: dict[str, str]) -> None:
    created = client.post(
        "/api/v1/channel-issuers",
        headers=scope,
        json={
            "channel": CHANNEL,
            "issuer": ISSUER,
            "public_key": RSA_PUBLIC_PEM,
            "allowed_origins": ["https://acme.example"],
        },
    )
    assert created.status_code == 201, created.text


@pytest.fixture
def published_agent(client: TestClient, scope: dict[str, str]) -> None:
    spec = {**VALID_SPEC, "end_user_access": {"enabled": True}}
    created = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Support Bot", "alias": ALIAS}
    )
    assert created.status_code == 201, created.text
    agent_id = str(created.json()["id"])
    draft = client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": spec},
    )
    assert draft.status_code == 200, draft.text
    published = client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    )
    assert published.status_code == 201, published.text


def _sign_in(client: TestClient, workspace_id: str, sub: str) -> UUID:
    exchanged = client.post(
        "/api/v1/end-user/sessions",
        headers={
            "Authorization": f"Bearer {_credential(workspace_id=workspace_id, sub=sub)}",
            "X-Workspace-Id": workspace_id,
        },
    )
    assert exchanged.status_code == 201, exchanged.text
    assert exchanged.cookies.get(END_USER_SESSION_COOKIE) is not None
    return UUID(exchanged.json()["end_user_id"])


def _start_session(client: TestClient) -> str:
    created = client.post(f"/api/v1/end-user/agents/{ALIAS}/sessions", json={})
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _submit_run(client: TestClient, session_id: str, key: str, text: str = "hello there") -> str:
    created = client.post(
        f"/api/v1/end-user/sessions/{session_id}/runs",
        headers={"Idempotency-Key": key},
        json={"input": text},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _cancel(client: TestClient, run_id: str, expected_state_version: int) -> object:
    return client.post(
        f"/api/v1/end-user/runs/{run_id}/cancel",
        json={"expected_state_version": expected_state_version},
    )


async def test_an_end_user_cancels_their_own_queued_run(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    run_id = _submit_run(client, session_id, "cancel-1")

    cancelled = _cancel(client, run_id, 1)

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["finished_at"] is not None


async def test_a_denied_cancel_audits_as_end_user_not_as_a_console_user(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: None,
    published_agent: None,
) -> None:
    """`a728f46` established `actor_type="end_user"` for end-user actions.
    `SqlRunStore.control_run` only writes to `audit_events` on the denied
    path (`test_run_control.py::test_illegal_control_is_refused_and_audited`
    pins the same thing for the console) — a successful control is recorded
    in `run_events` instead, not `audit_events` — so this is where an actor
    type actually lands, and where a copy-pasted `control_run` call reusing
    a console-shaped `CallerIdentity(USER, ...)` instead of
    `CallerIdentity(END_USER, ...)` would show up as `actor_type="user"`.
    """
    del registered_issuer, published_agent
    end_user_id = _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    run_id = _submit_run(client, session_id, "cancel-audit")

    first = _cancel(client, run_id, 1)
    assert first.status_code == 200, first.text
    # The run is now terminal (`cancelled`, version 2); cancelling it again
    # with the now-current version is a legal *read* of the version but an
    # illegal *transition*, which is exactly the shape that reaches
    # `control_run`'s denied branch rather than `StateVersionConflict`.
    denied = _cancel(client, run_id, 2)
    assert denied.status_code == 409, denied.text
    assert denied.json()["code"] == "invalid_state_transition"

    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT actor_type, actor_id FROM audit_events "
                    "WHERE resource_id = :run_id AND action = 'run.control_denied' "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"run_id": UUID(run_id)},
            )
        ).mappings().one()

    assert row["actor_type"] == "end_user"
    assert row["actor_id"] == end_user_id


async def test_cancelling_with_a_stale_state_version_conflicts(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    run_id = _submit_run(client, session_id, "cancel-2")

    refused = _cancel(client, run_id, 99)

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "state_version_conflict"


async def test_cancelling_another_end_users_run_is_refused(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    """§4.6: 本人 — only the Run's own end user, the same shape
    `test_end_user_reads.py::test_an_end_user_cannot_read_another_end_users_run`
    already pins for the read side."""
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")
    zhang_session = _start_session(client)
    zhang_run = _submit_run(client, zhang_session, "cancel-3")

    _sign_in(client, workspace_id, "li")
    refused = _cancel(client, zhang_run, 1)

    assert refused.status_code == 403, refused.text


async def test_cancelling_an_unknown_run_is_not_found(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")

    refused = _cancel(client, "00000000-0000-4000-8000-000000000000", 1)

    assert refused.status_code == 404, refused.text


async def test_an_unauthenticated_cancel_is_refused(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    run_id = _submit_run(client, session_id, "cancel-4")
    client.cookies.clear()

    refused = _cancel(client, run_id, 1)

    assert refused.status_code == 401, refused.text


#: Task-7 review finding 4's list, reused rather than redefined: the console's
#: `RunResponse` is the platform's own operational document, and a cancelled
#: Run reusing that shape would leak it here the same way `get_run` almost did
#: (`test_end_user_reads.py`).
_CONSOLE_ONLY_RUN_FIELDS = {
    "budget",
    "checkpoint_replay_safe",
    "checkpoint_effect_status",
    "checkpoint_usage_quality",
    "goal",
    "available_actions",
}


async def test_cancelling_does_not_leak_the_consoles_operational_fields(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    run_id = _submit_run(client, session_id, "cancel-5")

    cancelled = _cancel(client, run_id, 1)

    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert not (_CONSOLE_ONLY_RUN_FIELDS & set(body))
    assert body["state_version"] == 2


async def test_pause_has_no_end_user_route(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    """§10 builds only a third of §4.6's Run-control row, on purpose: the
    chat surface has no place for "pause", and cancel is the one both
    erasure and "I don't want this any more" need. Pinned here as a fact
    about the routing table, not left as a sentence a later change could
    contradict without a test noticing."""
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    run_id = _submit_run(client, session_id, "cancel-6")

    refused = client.post(
        f"/api/v1/end-user/runs/{run_id}/pause", json={"expected_state_version": 1}
    )

    assert refused.status_code == 404, refused.text


async def test_resume_has_no_end_user_route(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    run_id = _submit_run(client, session_id, "cancel-7")

    refused = client.post(
        f"/api/v1/end-user/runs/{run_id}/resume", json={"expected_state_version": 1}
    )

    assert refused.status_code == 404, refused.text
