"""§5's last bullet: `USER_CONFIRMATION` had a producer and no consumer.

`SqlApprovalGate._subject` has answered "who may confirm this" with `run.
end_user_id` since 0032, and `tool_answers.py` has opened a `user_confirmation`
instead of a `governance_approval` for a `caller_type=end_user` Run since the
same task. Neither mattered: `approval_routes.py` is `_CONSOLE_ONLY`, and
`reject_end_user_caller` refuses any request carrying the end-user cookie
before it gets near `may_decide`. Every governance-gated write an end user's
own Run made opened a confirmation nobody with that cookie could answer, and
it sat `waiting_approval` until the scheduler expired it.

This suite proves the door this task adds actually opens: an end user answers
their own `user_confirmation` and the write it was gating happens, exactly the
way `test_approvals.py` proves the console path does for a workspace admin's
`governance_approval`. It borrows that suite's real stand-in API and egress
proxy rather than a fake gate, because the finding these tests answer is that
the previous "verification" of a related behaviour turned out to be a
reviewer's scratch file — nothing here is worth writing unless it goes
through the real HTTP route, the real Worker, and the real database.

Design v2.5 §4.6's matrix draws the two directions this suite drills: a
`user_confirmation` is 仅发起人本人 (only the Run's own end user, never
another end user, however innocuous the mix-up would look from outside), and
a `governance_approval` never reaches an end user's decision at all, no matter
whose Run it is about. `runs/domain/approval.py::may_decide` already encodes
both; this suite is what proves the new route actually asks it rather than
re-deriving the rule at the edge.
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
from ..egress_support import ProxyHandle
from .http_tool_support import StandIn, approve_host, register_tool, worker

ISSUER = "https://idp.acme.example"
CHANNEL = "web"
ALIAS = "writer"


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
    """`https://testserver`, the same override `test_end_user_memory.py` and
    `test_end_user_subject_self_service.py` use: the end-user session cookie
    is `Secure` unconditionally (design §4.2)."""
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


def _agent(client: TestClient, scope: dict[str, str], version_id: str, write_policy: str) -> str:
    """A published Agent an end user may reach (§5's platform gate open) and
    that stops for a person before it writes (§16.3's `write_policy`)."""
    created = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Writer", "alias": ALIAS}
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
                "write_policy": write_policy,
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
    return agent_id


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


def _submit(client: TestClient, session_id: str, key: str) -> dict[str, Any]:
    created = client.post(
        f"/api/v1/end-user/sessions/{session_id}/runs",
        headers={"Idempotency-Key": key},
        json={"input": "http.orders.createOrder"},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


def _decide(
    client: TestClient, approval_id: str, decision: str, reason: str | None = None
) -> Any:
    return client.post(
        f"/api/v1/end-user/approvals/{approval_id}/decision",
        json={"decision": decision, "reason": reason},
    )


def _list_pending(client: TestClient) -> Any:
    return client.get("/api/v1/end-user/approvals")


async def _pending_confirmation(engine: AsyncEngine, run_id: str) -> dict[str, Any]:
    async with engine.connect() as connection:
        found = await connection.execute(
            text(
                "SELECT id, approval_type, status FROM approvals "
                "WHERE run_id = :run_id AND status = 'pending'"
            ),
            {"run_id": UUID(run_id)},
        )
        row = found.one()
    return {"id": str(row[0]), "approval_type": row[1], "status": row[2]}


async def _stopped_run(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
    *,
    sub: str,
) -> tuple[dict[str, Any], dict[str, Any], StandIn]:
    """小张 signs in, starts a Run that asks to write, and the Run stops
    waiting for his own confirmation. Returns the Run, the pending approval
    row, and the stand-in API nothing has reached yet."""
    stand_in, url = api
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url)
    _agent(client, scope, version_id, "governance")
    _sign_in(client, workspace_id, sub)
    session_id = _start_session(client)
    run = _submit(client, session_id, f"run-{sub}")

    await worker(engine, workspace_id, proxy).run_once()

    approval = await _pending_confirmation(engine, run["id"])
    return run, approval, stand_in


# -- the end user answers their own confirmation -----------------------------


async def test_the_write_opened_a_user_confirmation_not_a_governance_approval(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: None,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    del registered_issuer
    _run, approval, _stand_in = await _stopped_run(
        client, scope, workspace_id, engine, api, proxy, sub="zhang"
    )

    assert approval["approval_type"] == "user_confirmation"


async def test_the_end_user_approves_their_own_confirmation_and_the_write_happens(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: None,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    del registered_issuer
    run, approval, stand_in = await _stopped_run(
        client, scope, workspace_id, engine, api, proxy, sub="zhang"
    )

    decided = _decide(client, approval["id"], "approve")

    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"

    await worker(engine, workspace_id, proxy).run_once()

    assert stand_in.methods == ["POST"]
    async with engine.connect() as connection:
        status = await connection.execute(
            text("SELECT status FROM runs WHERE id = :id"), {"id": UUID(run["id"])}
        )
        assert status.scalar() == "completed"


async def test_the_end_user_rejects_their_own_confirmation_with_a_reason(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: None,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    del registered_issuer
    _run, approval, stand_in = await _stopped_run(
        client, scope, workspace_id, engine, api, proxy, sub="zhang"
    )

    decided = _decide(client, approval["id"], "reject", "not now")

    assert decided.status_code == 200, decided.text
    assert decided.json()["decision_reason"] == "not now"
    assert stand_in.requests == []


# -- the two refusals §4.6's matrix requires ---------------------------------


async def test_an_unauthenticated_request_is_refused(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: None,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """The route authenticates through `resolve_end_user_caller`, not through
    whatever console session happens to be on the request — there is none
    here, and no cookie at all, so it must not fall through to "not found"."""
    del registered_issuer
    _run, approval, _stand_in = await _stopped_run(
        client, scope, workspace_id, engine, api, proxy, sub="zhang"
    )
    client.cookies.clear()

    refused = _decide(client, approval["id"], "approve")

    assert refused.status_code == 401, refused.text


async def test_a_different_end_users_confirmation_stays_out_of_reach(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    settings: Settings,
    engine: AsyncEngine,
    registered_issuer: None,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """§4.6: 仅发起人本人. 小王 is a real, signed-in end user in the same
    workspace — not an anonymous caller — and still may not decide 小张's
    confirmation. A second cookie jar, the same way `test_end_user_memory.
    py`'s cross-subject test opens one for the second person."""
    del registered_issuer
    run, approval, stand_in = await _stopped_run(
        client, scope, workspace_id, engine, api, proxy, sub="zhang"
    )

    with TestClient(create_app(settings=settings), base_url="https://testserver") as wang_client:
        _sign_in(wang_client, workspace_id, "wang")

        refused = _decide(wang_client, approval["id"], "approve")

    assert refused.status_code == 403, refused.text
    async with engine.connect() as connection:
        status = await connection.execute(
            text("SELECT status FROM approvals WHERE id = :id"), {"id": UUID(approval["id"])}
        )
        assert status.scalar() == "pending"
    assert stand_in.requests == []
    del run


async def test_a_governance_approval_is_refused_to_an_end_user_regardless(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    registered_issuer: None,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """The other direction of §4.6's matrix: a `governance_approval` never
    reaches an end user's decision, no matter whose Run it is about — this
    one belongs to an ordinary workspace member's own Run, which has no
    end user at all, and the refusal must still be 403, never a crash on a
    Run with no `end_user_id` to compare against."""
    del registered_issuer
    stand_in, url = api
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url)
    agent_id = _agent(client, scope, version_id, "governance")
    session_id = session_for(agent_id)
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "console-write"},
        json={"session_id": session_id, "input": "http.orders.createOrder"},
    )
    assert created.status_code == 201, created.text

    await worker(engine, workspace_id, proxy).run_once()

    approval = await _pending_confirmation(engine, created.json()["id"])
    assert approval["approval_type"] == "governance_approval"

    _sign_in(client, workspace_id, "zhang")
    refused = _decide(client, approval["id"], "approve")

    assert refused.status_code == 403, refused.text
    assert stand_in.requests == []


# -- §10: the list a chat surface needs before it can show any of this ------
#
# `decide` above proves the door opens once an end user already has an
# `approval_id`. Nothing before this task ever gave them one — no list
# route, no id in any end-user response, `apps/chat-web` never mentioning
# "approval" at all — so the door was real and permanently unreached. These
# tests are the missing half: the same 仅发起人本人 rule §4.6 states for
# deciding, now checked on what a person is even allowed to *see*.


async def test_listing_returns_the_end_users_own_pending_confirmation(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: None,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    del registered_issuer
    run, approval, stand_in = await _stopped_run(
        client, scope, workspace_id, engine, api, proxy, sub="zhang"
    )

    listed = _list_pending(client)

    assert listed.status_code == 200, listed.text
    ids = [item["id"] for item in listed.json()]
    assert ids == [approval["id"]]
    assert listed.json()[0]["approval_type"] == "user_confirmation"
    assert listed.json()[0]["run_id"] == run["id"]
    del stand_in


async def test_listing_does_not_include_another_end_users_confirmation(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    settings: Settings,
    engine: AsyncEngine,
    registered_issuer: None,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """§4.6: 仅发起人本人 — 小王 signed in for real must still see nothing
    of 小张's pending confirmation, the same cross-subject shape
    `test_a_different_end_users_confirmation_stays_out_of_reach` already
    pins for deciding."""
    del registered_issuer
    _run, _approval, stand_in = await _stopped_run(
        client, scope, workspace_id, engine, api, proxy, sub="zhang"
    )

    with TestClient(create_app(settings=settings), base_url="https://testserver") as wang_client:
        _sign_in(wang_client, workspace_id, "wang")

        listed = _list_pending(wang_client)

    assert listed.status_code == 200, listed.text
    assert listed.json() == []
    del stand_in


async def test_listing_never_includes_a_governance_approval(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    session_for: Callable[[str], str],
    registered_issuer: None,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    """The other direction of §4.6's matrix, checked on the list this time:
    an end user's own list must never surface a `governance_approval`, even
    one sitting on a workspace member's own Run with no end user at all —
    mirroring `test_a_governance_approval_is_refused_to_an_end_user_regardless`
    on the read side rather than only the decide side."""
    del registered_issuer
    stand_in, url = api
    approve_host(client, scope, "127.0.0.1")
    version_id = register_tool(client, scope, url)
    agent_id = _agent(client, scope, version_id, "governance")
    session_id = session_for(agent_id)
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "console-write-2"},
        json={"session_id": session_id, "input": "http.orders.createOrder"},
    )
    assert created.status_code == 201, created.text

    await worker(engine, workspace_id, proxy).run_once()

    approval = await _pending_confirmation(engine, created.json()["id"])
    assert approval["approval_type"] == "governance_approval"

    _sign_in(client, workspace_id, "zhang")
    listed = _list_pending(client)

    assert listed.status_code == 200, listed.text
    assert approval["id"] not in [item["id"] for item in listed.json()]
    del stand_in


async def test_a_decided_confirmation_drops_off_the_list(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: None,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    del registered_issuer
    run, approval, stand_in = await _stopped_run(
        client, scope, workspace_id, engine, api, proxy, sub="zhang"
    )

    decided = _decide(client, approval["id"], "approve")
    assert decided.status_code == 200, decided.text

    listed = _list_pending(client)

    assert listed.status_code == 200, listed.text
    assert listed.json() == []
    del run, stand_in


async def test_an_unauthenticated_list_request_is_refused(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    engine: AsyncEngine,
    registered_issuer: None,
    api: tuple[StandIn, str],
    proxy: ProxyHandle,
) -> None:
    del registered_issuer
    _run, _approval, stand_in = await _stopped_run(
        client, scope, workspace_id, engine, api, proxy, sub="zhang"
    )
    client.cookies.clear()

    refused = _list_pending(client)

    assert refused.status_code == 401, refused.text
    del stand_in
