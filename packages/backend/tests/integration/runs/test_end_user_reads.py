"""The read half of §5's door: an end user watching their own conversation.

`end_user_run_router` (§5, wired in task 5) gave an end user exactly two
things: start a Session, submit a Run. Neither told them anything back —
`session_router`/`run_router` are `_CONSOLE_ONLY`, so nothing built for a
workspace member is reachable here, and the chat surface this task builds
has no way to show a reply without *some* door back onto the Run it
started and the Session it is talking through. This suite proves the two
new GET routes open exactly as far as `submit_end_user_run` already does —
this end user's own Session and Run, never another end user's guessed id —
reusing the ownership rule that route already enforces rather than
inventing a second one for reads.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.api.app import create_app
from tiny_hermes.identity.presentation.end_user_dependencies import END_USER_SESSION_COOKIE
from tiny_hermes.shared.config import Settings

from ..conftest import VALID_SPEC
from ..egress_support import ProxyHandle
from .http_tool_support import StandIn, approve_host, register_tool, worker

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
    """`https://testserver`, same override every end-user suite makes: the
    session cookie is `Secure` unconditionally and `httpx`'s jar only
    attaches a `Secure` cookie to an `https://` base URL."""
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


async def test_an_end_user_reads_their_own_session_messages(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    _submit_run(client, session_id, "reads-1")

    read = client.get(f"/api/v1/end-user/sessions/{session_id}/messages")

    assert read.status_code == 200, read.text
    assert read.json()[0]["parts"][0]["text"] == "hello there"


async def test_an_end_user_reads_their_own_run(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    run_id = _submit_run(client, session_id, "reads-2")

    read = client.get(f"/api/v1/end-user/runs/{run_id}")

    assert read.status_code == 200, read.text
    assert read.json()["id"] == run_id
    assert read.json()["session_id"] == session_id


#: Task-9 review finding F: `POST /end-user/agents/{alias}/sessions` returned
#: the console's own `SessionResponse` verbatim — `caller_type`, `caller_id`,
#: `head_run_id`, `next_run_sequence`, `next_message_sequence` are the
#: platform's own Session bookkeeping, the same kind of leak task-7 review
#: finding 4 already named for a Run and this task's own `EndUserRunResponse`
#: was narrowed to fix. `ChatPage.tsx` reads exactly one field off this
#: response — `created.id`, to start talking through it — so that is what
#: the narrowed model must carry and nothing past it.
_CONSOLE_ONLY_SESSION_FIELDS = {
    "caller_type",
    "caller_id",
    "head_run_id",
    "next_run_sequence",
    "next_message_sequence",
}


async def test_starting_a_session_does_not_leak_the_consoles_own_bookkeeping(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")

    created = client.post(f"/api/v1/end-user/agents/{ALIAS}/sessions", json={})

    assert created.status_code == 201, created.text
    body = created.json()
    assert not (_CONSOLE_ONLY_SESSION_FIELDS & set(body))
    assert "id" in body


#: Task-7 review finding 4: the console's `RunResponse` is the platform's
#: own operational document — budget consumption, checkpoint replay/
#: effect/usage internals, a goal round's outcome — none of which belongs
#: to another company's own employee just because this route happened to
#: reuse the console's response model. Named here so both routes below can
#: assert the same list of fields must never appear.
_CONSOLE_ONLY_RUN_FIELDS = {
    "budget",
    "checkpoint_replay_safe",
    "checkpoint_effect_status",
    "checkpoint_usage_quality",
    "goal",
    "available_actions",
}


async def test_reading_a_run_does_not_leak_the_consoles_operational_fields(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)
    run_id = _submit_run(client, session_id, "reads-4")

    read = client.get(f"/api/v1/end-user/runs/{run_id}")

    assert read.status_code == 200, read.text
    body = read.json()
    assert not (_CONSOLE_ONLY_RUN_FIELDS & set(body))
    # Still carries what a chat surface actually needs.
    assert body["id"] == run_id
    assert body["session_id"] == session_id
    assert "status" in body
    assert "finished_at" in body
    assert "queue" in body


async def test_submitting_a_run_does_not_leak_the_consoles_operational_fields(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    """The same leak on the write path — `create_run` returns the freshly
    submitted Run's own document, reusing the same console response model
    `get_run` did."""
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)

    submitted = client.post(
        f"/api/v1/end-user/sessions/{session_id}/runs",
        headers={"Idempotency-Key": "reads-5"},
        json={"input": "hello there"},
    )

    assert submitted.status_code == 201, submitted.text
    body = submitted.json()
    assert not (_CONSOLE_ONLY_RUN_FIELDS & set(body))


def _governance_agent(client: TestClient, scope: dict[str, str], version_id: str) -> None:
    """A published Agent, reachable through `ALIAS` (so the existing
    `_credential` helper's `agents` claim still names it), that stops for the
    end user's own confirmation before it writes — the same `write_policy:
    governance` shape `test_end_user_approvals.py` uses to reach
    `waiting_approval`. That state is what a second Run submitted on the same
    Session sees as a blocked queue below.
    """
    created = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Support Bot", "alias": ALIAS}
    )
    assert created.status_code == 201, created.text
    agent_id = str(created.json()["id"])
    spec = {
        **VALID_SPEC,
        "model_policy": {"provider": "deterministic", "scenario": "http_once"},
        "network": {"allow": ["127.0.0.1"]},
        "end_user_access": {"enabled": True},
        "http_tools": [
            {
                "http_tool_version_id": version_id,
                "operations": ["listOrders", "createOrder"],
                "write_policy": "governance",
            }
        ],
    }
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


async def test_a_blocked_end_user_queue_carries_only_position_and_status(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: None,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """Task-7 review finding 4 missed one nesting level: `EndUserRunResponse.
    queue` reused the console's `QueueResponse` unchanged, and that class
    still names `available_actions` — console-style action names computed
    with `can_control=True`. The field is dormant until `queue.status ==
    "session_blocked"`, and that status is ordinary for this audience: an end
    user's own Run sitting in `waiting_approval` blocks its Session's head
    exactly as a paused console Run does, and `WAITING_APPROVAL` is squarely
    reachable — the end user's own approval route puts Runs there. This test
    drives a real Run into that state through the same governance
    write-policy path `test_end_user_approvals.py` uses, then submits a
    second Run behind it, so the blocked queue it reads back is the one an
    end user's own session actually produces — not a hand-built model that
    would never have shown the leak in the first place.
    """
    del registered_issuer
    stand_in, url = api
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url)
    _governance_agent(client, scope, version_id)
    _sign_in(client, workspace_id, "zhang")
    session_id = _start_session(client)

    head = client.post(
        f"/api/v1/end-user/sessions/{session_id}/runs",
        headers={"Idempotency-Key": "head"},
        json={"input": "http.orders.createOrder"},
    )
    assert head.status_code == 201, head.text

    await worker(engine, workspace_id, proxy).run_once()

    second = client.post(
        f"/api/v1/end-user/sessions/{session_id}/runs",
        headers={"Idempotency-Key": "second"},
        json={"input": "hello"},
    )
    assert second.status_code == 201, second.text
    second_body = second.json()

    assert second_body["queue"]["status"] == "session_blocked"
    assert second_body["queue"] == {"position": 2, "status": "session_blocked"}

    read = client.get(f"/api/v1/end-user/runs/{second_body['id']}")
    assert read.status_code == 200, read.text
    assert read.json()["queue"] == {"position": 2, "status": "session_blocked"}
    del stand_in


async def test_an_end_user_cannot_read_another_end_users_session(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    """The read side of the same ownership rule `submit_end_user_run`
    already enforces on the write — a guessed `session_id` must not open
    somebody else's conversation just because the reader has a live session
    of their own."""
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")
    zhang_session = _start_session(client)

    _sign_in(client, workspace_id, "li")
    read = client.get(f"/api/v1/end-user/sessions/{zhang_session}/messages")

    assert read.status_code == 403, read.text


async def test_an_end_user_cannot_read_another_end_users_run(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")
    zhang_session = _start_session(client)
    zhang_run = _submit_run(client, zhang_session, "reads-3")

    _sign_in(client, workspace_id, "li")
    read = client.get(f"/api/v1/end-user/runs/{zhang_run}")

    assert read.status_code == 403, read.text


async def test_an_unknown_session_id_is_refused_not_found_shaped(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    published_agent: None,
) -> None:
    del registered_issuer, published_agent
    _sign_in(client, workspace_id, "zhang")

    read = client.get(
        "/api/v1/end-user/sessions/00000000-0000-4000-8000-000000000000/messages"
    )

    assert read.status_code == 404, read.text
