"""§5's two-gate check, at the HTTP layer it is actually reached through.

`tests/unit/agents/test_end_user_access.py` already drills all four
combinations of `AgentCatalog.resolve_end_user_agent` directly. What it
cannot see is `end_user_routes.py::_resolve_agent`'s own job: translating the
two refusals into two different HTTP bodies, one of which must say which
Agent and one of which must say nothing at all. A regression there — the two
`except` clauses swapped, or `str(error)` used where the fixed string
belongs — would not fail a single domain test and would leak an alias to an
end user who was never assigned it. This suite is the thing that would catch
it.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from tiny_hermes.api.app import create_app
from tiny_hermes.identity.presentation.end_user_dependencies import END_USER_SESSION_COOKIE
from tiny_hermes.shared.config import Settings

from ..conftest import VALID_SPEC

ISSUER = "https://idp.acme.example"
CHANNEL = "web"
#: Two aliases, two Agents, two gate states — see the fixtures below.
LISTED_BUT_CLOSED = "closed-door"
OPEN_BUT_UNLISTED = "open-door"


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


def _credential(*, workspace_id: str, sub: str, agents: list[str]) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "sub": sub,
        "aud": workspace_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "agents": agents,
    }
    return jwt.encode(payload, RSA_PRIVATE_PEM, algorithm="RS256")


@pytest.fixture
def client(empty_database: None, settings: Settings) -> Iterator[TestClient]:
    """Same override as `test_end_user_memory.py`: the end-user session
    cookie is `Secure` unconditionally, and `httpx`'s jar only attaches a
    `Secure` cookie to an `https://` base URL."""
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


def _publish(
    client: TestClient, scope: dict[str, str], *, alias: str, end_user_access_enabled: bool
) -> None:
    spec = {**VALID_SPEC, "end_user_access": {"enabled": end_user_access_enabled}}
    created = client.post(
        "/api/v1/agents", headers=scope, json={"name": alias.title(), "alias": alias}
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


@pytest.fixture
def both_agents(client: TestClient, scope: dict[str, str]) -> None:
    """One Agent whose author never opened §5's platform gate, one whose
    author did. The credential's own `agents` claim is what decides which of
    the two failure modes each request below exercises."""
    _publish(client, scope, alias=LISTED_BUT_CLOSED, end_user_access_enabled=False)
    _publish(client, scope, alias=OPEN_BUT_UNLISTED, end_user_access_enabled=True)


def _sign_in(client: TestClient, workspace_id: str, sub: str, agents: list[str]) -> None:
    exchanged = client.post(
        "/api/v1/end-user/sessions",
        headers={
            "Authorization": f"Bearer {_credential(workspace_id=workspace_id, sub=sub, agents=agents)}",
            "X-Workspace-Id": workspace_id,
        },
    )
    assert exchanged.status_code == 201, exchanged.text
    assert exchanged.cookies.get(END_USER_SESSION_COOKIE) is not None


async def test_listed_but_gate_closed_names_the_alias_as_the_admins_problem(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    both_agents: None,
) -> None:
    """The credential's employer already knows the alias — it put it there —
    so naming it back costs nothing and tells the workspace admin exactly
    which switch to flip."""
    del registered_issuer, both_agents
    _sign_in(client, workspace_id, "zhang", agents=[LISTED_BUT_CLOSED])

    refused = client.post(f"/api/v1/end-user/agents/{LISTED_BUT_CLOSED}/sessions", json={})

    assert refused.status_code == 403, refused.text
    body = refused.json()
    assert body["code"] == "end_user_access_gate_closed"
    assert LISTED_BUT_CLOSED in body["detail"]


async def test_gate_open_but_unlisted_says_nothing_the_end_user_could_learn_from(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    both_agents: None,
) -> None:
    """Design §8: an end user calling an Agent nobody assigned them must
    learn nothing past "no" — not even that the alias exists and is merely
    unassigned to them. The regression this guards against is `str(error)`
    sneaking into the detail, since `EndUserAccessNotAssigned`'s own message
    embeds the alias for server-side logs."""
    del registered_issuer, both_agents
    _sign_in(client, workspace_id, "zhang", agents=["something-else"])

    refused = client.post(f"/api/v1/end-user/agents/{OPEN_BUT_UNLISTED}/sessions", json={})

    assert refused.status_code == 403, refused.text
    body = refused.json()
    assert body["code"] == "end_user_agent_not_found"
    assert OPEN_BUT_UNLISTED not in body["detail"]
    assert OPEN_BUT_UNLISTED not in refused.text
