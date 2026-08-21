"""Design §7's origin check, at the HTTP layer it actually guards.

`test_end_user_dependencies.py` already drills `enforce_end_user_origin`
directly. What it cannot see is whether the real write routes actually call
it — `resolve_end_user_caller_for_write` wired into the wrong route, or one
route quietly left on the plain `resolve_end_user_caller`, would not fail a
single unit test and would leave exactly the hole the brief describes: a
third-party page attaching the `SameSite=None` cookie to a state-changing
request. This suite is the one that would catch it, over a route that
creates an end-user Session — the least dangerous of the writes design §7
names, chosen because it needs no Run or approval scaffolding to set up.
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
#: A second issuer in the same workspace, used only by
#: `test_a_write_from_a_different_issuers_origin_in_the_same_workspace_is_refused`
#: to prove finding 3's fix: the origin check is scoped to the issuer that
#: minted *this* session, not unioned across every active issuer the
#: workspace has registered.
ISSUER_B = "https://idp-b.acme.example"
CHANNEL = "web"
ALLOWED_ORIGIN = "https://acme.example"
ALLOWED_ORIGIN_B = "https://acme-b.example"
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
RSA_PRIVATE_PEM_B, RSA_PUBLIC_PEM_B = _rsa_keypair()


def _credential(
    *, workspace_id: str, sub: str, iss: str = ISSUER, key: str = RSA_PRIVATE_PEM
) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": iss,
        "sub": sub,
        "aud": workspace_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "agents": [ALIAS],
    }
    return jwt.encode(payload, key, algorithm="RS256")


@pytest.fixture
def client(empty_database: None, settings: Settings) -> Iterator[TestClient]:
    """Same override as `test_end_user_agent_gates.py`: the end-user session
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
            "allowed_origins": [ALLOWED_ORIGIN],
        },
    )
    assert created.status_code == 201, created.text


@pytest.fixture
def second_registered_issuer(client: TestClient, scope: dict[str, str]) -> None:
    """A second, independent issuer in the *same* workspace, registering a
    *different* origin — the shape finding 3's fix has to tell apart from
    `registered_issuer`."""
    created = client.post(
        "/api/v1/channel-issuers",
        headers=scope,
        json={
            "channel": CHANNEL,
            "issuer": ISSUER_B,
            "public_key": RSA_PUBLIC_PEM_B,
            "allowed_origins": [ALLOWED_ORIGIN_B],
        },
    )
    assert created.status_code == 201, created.text


@pytest.fixture
def open_agent(client: TestClient, scope: dict[str, str]) -> None:
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


def _sign_in(
    client: TestClient,
    workspace_id: str,
    sub: str = "zhang",
    *,
    iss: str = ISSUER,
    key: str = RSA_PRIVATE_PEM,
) -> None:
    credential = _credential(workspace_id=workspace_id, sub=sub, iss=iss, key=key)
    exchanged = client.post(
        "/api/v1/end-user/sessions",
        headers={
            "Authorization": f"Bearer {credential}",
            "X-Workspace-Id": workspace_id,
        },
    )
    assert exchanged.status_code == 201, exchanged.text
    assert exchanged.cookies.get(END_USER_SESSION_COOKIE) is not None


async def test_a_write_from_an_unregistered_origin_is_refused(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    open_agent: None,
) -> None:
    """The brief's own scenario: a hostile page cannot ride the cookie in
    just because it can make the browser attach it."""
    del registered_issuer, open_agent
    _sign_in(client, workspace_id)

    refused = client.post(
        f"/api/v1/end-user/agents/{ALIAS}/sessions",
        json={},
        headers={"Origin": "https://evil.example"},
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "end_user_origin_not_allowed"


async def test_a_write_from_the_registered_origin_still_succeeds(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    open_agent: None,
) -> None:
    """The check must not be a blanket refusal — the origin the workspace
    itself registered has to keep working."""
    del registered_issuer, open_agent
    _sign_in(client, workspace_id)

    created = client.post(
        f"/api/v1/end-user/agents/{ALIAS}/sessions",
        json={},
        headers={"Origin": ALLOWED_ORIGIN},
    )

    assert created.status_code == 201, created.text


async def test_a_write_with_no_origin_or_referer_still_succeeds(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    open_agent: None,
) -> None:
    """No cross-site evidence at all is not refused — see
    `enforce_end_user_origin`'s own docstring for why."""
    del registered_issuer, open_agent
    _sign_in(client, workspace_id)

    created = client.post(f"/api/v1/end-user/agents/{ALIAS}/sessions", json={})

    assert created.status_code == 201, created.text


async def test_a_write_from_a_different_issuers_origin_in_the_same_workspace_is_refused(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: None,
    second_registered_issuer: None,
    open_agent: None,
) -> None:
    """Task-7 review finding 3, over real HTTP: two active issuers in one
    workspace, each registering a different origin. A session minted
    through issuer A's credential must not be usable from a page served at
    issuer B's origin, even though issuer B is perfectly legitimate in this
    same workspace — the old union-across-the-workspace check would have
    let this through."""
    del registered_issuer, second_registered_issuer, open_agent
    _sign_in(client, workspace_id, "zhang", iss=ISSUER, key=RSA_PRIVATE_PEM)

    refused = client.post(
        f"/api/v1/end-user/agents/{ALIAS}/sessions",
        json={},
        headers={"Origin": ALLOWED_ORIGIN_B},
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "end_user_origin_not_allowed"

    # Sanity: this end user's own issuer's origin still works.
    allowed = client.post(
        f"/api/v1/end-user/agents/{ALIAS}/sessions",
        json={},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert allowed.status_code == 201, allowed.text
