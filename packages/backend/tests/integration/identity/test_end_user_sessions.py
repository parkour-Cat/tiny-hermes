"""Design §4.2, §4.3, and §8, end to end: registering an issuer, exchanging
a credential for a session cookie, revoking it, and the boundary that keeps
an end user off every console endpoint.

`client` here is not the module-level fixture from `tests/integration/conftest.py`
— it is redefined below with `base_url="https://testserver"`. The exchange
endpoint's cookie is `Secure` unconditionally (design §4.2: it is always read
cross-origin, so there is no "development" exemption), and `httpx`'s cookie
jar — correctly — refuses to attach a `Secure` cookie to a request made over
`http://`. `https://testserver` is still the in-process ASGI transport, no
socket and no real TLS; it only changes which scheme `httpx` believes the
request used, which is what makes the jar carry the cookie on the next
request the way a real browser would.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

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

ISSUER = "https://idp.acme.example"
CHANNEL = "web"


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
_OTHER_PRIVATE_PEM, _OTHER_PUBLIC_PEM = _rsa_keypair()


def _credential(
    *,
    workspace_id: str,
    sub: str = "acme-user-42",
    exp_minutes: float = 10,
    key: str = RSA_PRIVATE_PEM,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "sub": sub,
        "aud": workspace_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=exp_minutes)).timestamp()),
        "agents": ["support-bot"],
    }
    return jwt.encode(payload, key, algorithm="RS256")


# -- fixtures: an https-scheme client so Secure cookies round-trip ----------


@pytest.fixture
def client(empty_database: None, settings: Settings) -> Iterator[TestClient]:
    del empty_database
    with TestClient(create_app(settings=settings), base_url="https://testserver") as value:
        yield value


@pytest.fixture
def registered_issuer(
    client: TestClient, scope: dict[str, str]
) -> Callable[..., dict[str, object]]:
    def register(
        *, public_key: str | None = RSA_PUBLIC_PEM, jwks_url: str | None = None
    ) -> dict[str, object]:
        created = client.post(
            "/api/v1/channel-issuers",
            headers=scope,
            json={
                "channel": CHANNEL,
                "issuer": ISSUER,
                "public_key": public_key,
                "jwks_url": jwks_url,
                "allowed_origins": ["https://acme.example"],
            },
        )
        assert created.status_code == 201, created.text
        return dict(created.json())

    return register


def _exchange(client: TestClient, workspace_id: str, token: str):  # noqa: ANN201
    return client.post(
        "/api/v1/end-user/sessions",
        headers={"Authorization": f"Bearer {token}", "X-Workspace-Id": workspace_id},
    )


# -- a full exchange ----------------------------------------------------


def test_a_registered_issuers_credential_exchanges_for_a_session(
    client: TestClient, workspace_id: str, registered_issuer: Callable[..., dict[str, object]]
) -> None:
    registered_issuer()

    response = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))

    assert response.status_code == 201, response.text
    body = response.json()
    assert "end_user_id" in body
    assert "expires_at" in body
    cookie = response.cookies.get(END_USER_SESSION_COOKIE)
    assert cookie is not None
    set_cookie_header = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie_header
    assert "Secure" in set_cookie_header
    assert "samesite=none" in set_cookie_header.lower()


# -- idempotent upsert, design §282 --------------------------------------


def test_the_same_sub_exchanging_twice_gets_the_same_end_user_id(
    client: TestClient, workspace_id: str, registered_issuer: Callable[..., dict[str, object]]
) -> None:
    registered_issuer()

    first = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))
    second = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))

    assert first.json()["end_user_id"] == second.json()["end_user_id"]


def test_a_different_sub_gets_a_different_end_user_id(
    client: TestClient, workspace_id: str, registered_issuer: Callable[..., dict[str, object]]
) -> None:
    registered_issuer()

    first = _exchange(client, workspace_id, _credential(workspace_id=workspace_id, sub="user-a"))
    second = _exchange(client, workspace_id, _credential(workspace_id=workspace_id, sub="user-b"))

    assert first.json()["end_user_id"] != second.json()["end_user_id"]


# -- refusals, design §8 --------------------------------------------------


def test_an_unregistered_issuer_is_refused(client: TestClient, workspace_id: str) -> None:
    response = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))

    assert response.status_code == 401
    assert response.json()["code"] == "end_user_credential_invalid"


def test_a_credential_past_the_15_minute_ceiling_names_the_specific_reason(
    client: TestClient, workspace_id: str, registered_issuer: Callable[..., dict[str, object]]
) -> None:
    registered_issuer()

    response = _exchange(
        client, workspace_id, _credential(workspace_id=workspace_id, exp_minutes=60)
    )

    assert response.status_code == 401
    assert response.json()["code"] == "end_user_credential_lifetime_exceeds_ceiling"


def test_a_bad_signature_is_refused_generically(
    client: TestClient, workspace_id: str, registered_issuer: Callable[..., dict[str, object]]
) -> None:
    registered_issuer()

    response = _exchange(
        client,
        workspace_id,
        _credential(workspace_id=workspace_id, key=_OTHER_PRIVATE_PEM),
    )

    assert response.status_code == 401
    assert response.json()["code"] == "end_user_credential_invalid"


async def test_an_erased_subjects_credential_cannot_exchange_for_a_session(
    client: TestClient,
    workspace_id: str,
    registered_issuer: Callable[..., dict[str, object]],
    engine: AsyncEngine,
) -> None:
    registered_issuer()
    first = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))
    end_user_id = first.json()["end_user_id"]

    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE end_users SET erased_at = now() WHERE id = :id"), {"id": end_user_id}
        )

    response = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))

    assert response.status_code == 401
    assert response.json()["code"] == "end_user_credential_invalid"


# -- disabling an issuer: new credentials refused, live sessions unaffected --


async def test_disabling_an_issuer_refuses_new_credentials_but_not_the_already_exchanged_session(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: Callable[..., dict[str, object]],
    engine: AsyncEngine,
) -> None:
    issuer = registered_issuer()
    already_exchanged = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))
    assert already_exchanged.status_code == 201
    end_user_id = already_exchanged.json()["end_user_id"]

    disabled = client.post(f"/api/v1/channel-issuers/{issuer['id']}/disable", headers=scope)
    assert disabled.status_code == 200, disabled.text

    new_attempt = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))
    assert new_attempt.status_code == 401

    # design §4.3's documented trade-off, asserted as the known behaviour it
    # is rather than something to fix: disabling the issuer stops *new*
    # credentials but leaves an already-exchanged session row alone — there
    # is no end-user-facing protected route yet to prove this against over
    # HTTP (that lands in a later task), so this checks the row itself.
    async with engine.connect() as connection:
        revoked_at = (
            await connection.execute(
                text("SELECT revoked_at FROM end_user_sessions WHERE end_user_id = :id"),
                {"id": end_user_id},
            )
        ).scalar_one()
    assert revoked_at is None


# -- revocation, design §4.3 -----------------------------------------------


def test_revoking_an_end_users_sessions_invalidates_the_cookie_immediately(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: Callable[..., dict[str, object]],
) -> None:
    registered_issuer()
    exchanged = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))
    end_user_id = exchanged.json()["end_user_id"]

    revoked = client.delete(f"/api/v1/end-user/sessions/{end_user_id}", headers=scope)

    assert revoked.status_code == 204


async def test_a_workspace_viewer_cannot_revoke_end_user_sessions(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: Callable[..., dict[str, object]],
    engine: AsyncEngine,
) -> None:
    registered_issuer()
    exchanged = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))
    end_user_id = exchanged.json()["end_user_id"]
    # The console guard (design §8) does not care who else is signed in —
    # holding the end-user cookie at all is enough to be refused, which is
    # exactly what the next few admin calls would hit if it stayed.
    del client.cookies[END_USER_SESSION_COOKIE]

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (id, status, display_name, is_platform_admin, created_at) "
                "VALUES (gen_random_uuid(), 'active', 'Viewer', false, now())"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO auth_identities "
                "(id, user_id, provider, subject, password_hash, created_at) "
                "SELECT gen_random_uuid(), id, 'local', 'viewer@example.com', "
                "  (SELECT password_hash FROM auth_identities LIMIT 1), now() "
                "FROM users WHERE display_name = 'Viewer'"
            )
        )
    invited = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=scope,
        json={"email": "viewer@example.com", "role": "viewer"},
    )
    assert invited.status_code == 201

    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/sessions",
        json={"subject": "viewer@example.com", "password": "long-pass-123"},
    )
    assert login.status_code == 201
    csrf = login.cookies["tiny_hermes_csrf"]

    response = client.delete(
        f"/api/v1/end-user/sessions/{end_user_id}",
        headers={"X-Workspace-Id": workspace_id, "X-CSRF-Token": csrf},
    )

    assert response.status_code == 403


# -- the console boundary, design §8's last row ----------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/agents"),
        ("GET", "/api/v1/sessions"),
        ("GET", "/api/v1/memories/pending"),
    ],
)
def test_an_end_user_session_cookie_cannot_reach_console_endpoints(
    client: TestClient,
    workspace_id: str,
    registered_issuer: Callable[..., dict[str, object]],
    method: str,
    path: str,
) -> None:
    registered_issuer()
    exchanged = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))
    assert exchanged.status_code == 201

    response = client.request(
        method, path, headers={"X-Workspace-Id": workspace_id}
    )

    assert response.status_code == 403, response.text
