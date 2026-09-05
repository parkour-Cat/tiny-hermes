"""`GET /api/v1/end-user/agents`: which Agents this end user's own session may
open, with the names a person recognises.

The chat page used to print the alias in its title because an end user had
no way to ask what the Agent was called, and could not switch between the
Agents their credential named because nothing listed them. This route
answers both from the same two gates `resolve_end_user_agent` already
applies to one alias — it is that check, run over the credential's own
`agents` list, never a workspace listing.
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
OPEN_ALIAS = "support-bot"
CLOSED_ALIAS = "dormant-bot"
GHOST_ALIAS = "ghost-bot"


def _rsa_keypair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


RSA_PRIVATE_PEM, RSA_PUBLIC_PEM = _rsa_keypair()


def _credential(*, workspace_id: str, agents: list[str]) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "sub": "zhang",
        "aud": workspace_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "agents": agents,
    }
    return jwt.encode(payload, RSA_PRIVATE_PEM, algorithm="RS256")


@pytest.fixture
def client(empty_database: None, settings: Settings) -> Iterator[TestClient]:
    del empty_database
    with TestClient(create_app(settings=settings), base_url="https://testserver") as value:
        yield value


@pytest.fixture
def registered_issuer(client: TestClient, scope: dict[str, str]) -> None:
    created = client.post(
        "/api/v1/channel-issuers",
        headers=scope,
        json={
            "channel": "web",
            "issuer": ISSUER,
            "public_key": RSA_PUBLIC_PEM,
            "allowed_origins": ["https://acme.example"],
        },
    )
    assert created.status_code == 201, created.text


def _publish(
    client: TestClient, scope: dict[str, str], name: str, alias: str, *, open_to_end_users: bool
) -> None:
    spec = {**VALID_SPEC, "end_user_access": {"enabled": True}} if open_to_end_users else VALID_SPEC
    created = client.post("/api/v1/agents", headers=scope, json={"name": name, "alias": alias})
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


def _sign_in(client: TestClient, workspace_id: str, agents: list[str]) -> None:
    exchanged = client.post(
        "/api/v1/end-user/sessions",
        headers={
            "Authorization": f"Bearer {_credential(workspace_id=workspace_id, agents=agents)}",
            "X-Workspace-Id": workspace_id,
        },
    )
    assert exchanged.status_code == 201, exchanged.text
    assert exchanged.cookies.get(END_USER_SESSION_COOKIE) is not None


async def test_an_end_user_is_told_which_agents_they_may_open_and_what_they_are_called(
    client: TestClient, scope: dict[str, str], workspace_id: str, registered_issuer: None
) -> None:
    """凭据点了三个别名：一个开了终端用户入口，一个发布了但没开，一个根本不存在。
    只有第一个回来，而且带着人看得懂的名字。

    没开的那个不回：§5 的两道门缺一道就等于关着，把它列出来只会让人点进一个
    注定 403 的入口。不存在的那个不回也不报错：凭据是企业签的，它点错名字不是
    这个终端用户能修的事，也不该让他从这里学到工作空间里有什么。
    """
    del registered_issuer
    _publish(client, scope, "Support Bot", OPEN_ALIAS, open_to_end_users=True)
    _publish(client, scope, "Dormant Bot", CLOSED_ALIAS, open_to_end_users=False)
    _sign_in(client, workspace_id, [OPEN_ALIAS, CLOSED_ALIAS, GHOST_ALIAS])

    listed = client.get("/api/v1/end-user/agents")

    assert listed.status_code == 200, listed.text
    assert listed.json() == [{"alias": OPEN_ALIAS, "name": "Support Bot"}]


async def test_without_a_session_there_is_nobody_to_list_agents_for(
    client: TestClient, scope: dict[str, str], workspace_id: str, registered_issuer: None
) -> None:
    del registered_issuer, scope, workspace_id
    refused = client.get("/api/v1/end-user/agents")
    assert refused.status_code == 401, refused.text
