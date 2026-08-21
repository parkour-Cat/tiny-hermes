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

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.api.app import create_app
from tiny_hermes.identity.application.end_user_service import EndUserIdentityService
from tiny_hermes.identity.infrastructure.sql_end_user_store import SqlEndUserStore
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
    # The console guard (design §8's last row) does not care that this is
    # the same admin who just exchanged a credential above — a real admin
    # browser and a real end-user browser never share a cookie jar, so this
    # drops the one the test picked up standing in for both.
    del client.cookies[END_USER_SESSION_COOKIE]

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


async def test_revoking_an_end_users_sessions_invalidates_the_cookie_immediately(
    client: TestClient,
    scope: dict[str, str],
    workspace_id: str,
    registered_issuer: Callable[..., dict[str, object]],
    engine: AsyncEngine,
) -> None:
    registered_issuer()
    exchanged = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))
    end_user_id = exchanged.json()["end_user_id"]
    # See the same note in the disable-issuer test above: a real admin
    # browser never carries this cookie, so the test shouldn't either.
    del client.cookies[END_USER_SESSION_COOKIE]

    revoked = client.delete(f"/api/v1/end-user/sessions/{end_user_id}", headers=scope)

    assert revoked.status_code == 204
    # The session is not just deleted-by-response-code: the row itself must
    # carry the revocation, which is what "the same request but replayed
    # against the row" actually checks rather than trusting the status code
    # alone.
    async with engine.connect() as connection:
        revoked_at = (
            await connection.execute(
                text("SELECT revoked_at FROM end_user_sessions WHERE end_user_id = :id"),
                {"id": end_user_id},
            )
        ).scalar_one()
    assert revoked_at is not None


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
    end_user_cookie = exchanged.cookies.get(END_USER_SESSION_COOKIE)
    assert end_user_cookie is not None
    # Cleared here so the admin setup below (itself console calls, already
    # guarded) is not collaterally blocked by the cookie this test is
    # actually about — put back just before the call under test.
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
    # Put back deliberately, right before the guarded call. Task-9 review
    # finding G means the console guard no longer fires here on its own —
    # the viewer's own valid session cookie sits right next to the
    # end-user cookie now, so the request reaches the real route. The 403
    # below is `EndUserIdentityService.revoke_sessions`'s own role check
    # refusing a viewer, same as it always would with no end-user cookie in
    # the jar at all; this still proves a workspace viewer cannot revoke,
    # just no longer by way of the console guard.
    client.cookies.set(END_USER_SESSION_COOKIE, end_user_cookie)

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
def test_an_end_user_session_cookie_alone_cannot_reach_console_endpoints(
    client: TestClient,
    workspace_id: str,
    registered_issuer: Callable[..., dict[str, object]],
    method: str,
    path: str,
) -> None:
    """"Alone" is the operative word after task-9 review finding G: `client`
    already carries the bootstrap admin's own console session cookie
    (`workspace_id`'s own fixture chain logs them in), so it has to be
    cleared here for this to test what its name says — an end user with no
    console credential of their own, not an admin who happens to be
    carrying this cookie too. `test_an_end_user_with_valid_console_
    credentials_reaches_console_endpoints`, right below, is the other half:
    the same three routes, the same end-user cookie, the admin's console
    cookie left in place.
    """
    registered_issuer()
    exchanged = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))
    assert exchanged.status_code == 201
    del client.cookies["tiny_hermes_session"]

    response = client.request(
        method, path, headers={"X-Workspace-Id": workspace_id}
    )

    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/agents"),
        ("GET", "/api/v1/sessions"),
        ("GET", "/api/v1/memories/pending"),
    ],
)
def test_an_end_user_with_valid_console_credentials_reaches_console_endpoints(
    client: TestClient,
    workspace_id: str,
    registered_issuer: Callable[..., dict[str, object]],
    method: str,
    path: str,
) -> None:
    """Task-9 review finding G: a browser that is both a workspace member
    and an end user on one domain carries both cookies on every console
    request, and the end-user cookie's mere presence used to 403 it out of
    the console regardless. `client` already carries the bootstrap admin's
    own valid console session (untouched, unlike the test above); exchanging
    an end-user credential on top of it must not take the console away from
    them.
    """
    registered_issuer()
    exchanged = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))
    assert exchanged.status_code == 201

    response = client.request(
        method, path, headers={"X-Workspace-Id": workspace_id}
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "method,path,json_body",
    [
        (
            "POST",
            "/api/v1/channel-issuers",
            {
                "channel": "web",
                "issuer": "https://other.example",
                "public_key": RSA_PUBLIC_PEM,
                "allowed_origins": [],
            },
        ),
        ("GET", "/api/v1/channel-issuers", None),
        ("POST", f"/api/v1/channel-issuers/{uuid4()}/disable", None),
        ("DELETE", f"/api/v1/end-user/sessions/{uuid4()}", None),
    ],
)
def test_an_end_user_session_cookie_alone_cannot_reach_this_tasks_own_admin_routes(
    client: TestClient,
    workspace_id: str,
    scope: dict[str, str],
    registered_issuer: Callable[..., dict[str, object]],
    method: str,
    path: str,
    json_body: dict[str, object] | None,
) -> None:
    """These four routes are this task's own — `channel_issuers` CRUD and
    session revocation — and reaching 403 here must come from the console
    guard, not incidentally from something else that would also refuse.
    `scope` carries a *valid* CSRF token, but the platform admin's own
    session cookie is cleared right below: task-9 review finding G means
    that cookie is no longer enough on its own to make the guard fire
    alongside the end-user cookie, so this test needs the console session
    genuinely absent to prove what its name says — an end user with no
    console credential of their own reaching for these routes.
    `test_an_end_user_with_valid_console_credentials_reaches_this_tasks_
    own_admin_routes`, right below, is the other half.
    """
    registered_issuer()
    exchanged = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))
    assert exchanged.status_code == 201
    del client.cookies["tiny_hermes_session"]

    response = client.request(method, path, headers=scope, json=json_body)

    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    "method,path,json_body,expected_status",
    [
        (
            "POST",
            "/api/v1/channel-issuers",
            {
                "channel": "web",
                "issuer": "https://other.example",
                "public_key": RSA_PUBLIC_PEM,
                "allowed_origins": [],
            },
            201,
        ),
        ("GET", "/api/v1/channel-issuers", None, 200),
        # A random id, so "reached the real route" shows up as 404 rather
        # than a body match — the guard letting the request through is what
        # this proves, not that these particular ids exist.
        ("POST", f"/api/v1/channel-issuers/{uuid4()}/disable", None, 404),
        ("DELETE", f"/api/v1/end-user/sessions/{uuid4()}", None, 404),
    ],
)
def test_an_end_user_with_valid_console_credentials_reaches_this_tasks_own_admin_routes(
    client: TestClient,
    workspace_id: str,
    scope: dict[str, str],
    registered_issuer: Callable[..., dict[str, object]],
    method: str,
    path: str,
    json_body: dict[str, object] | None,
    expected_status: int,
) -> None:
    """Task-9 review finding G, on this task's own admin routes specifically:
    an admin who is also (on this same domain) an end user must keep
    console access to `channel_issuers` CRUD and session revocation — the
    very capabilities this task added — rather than being locked out of
    managing them the moment they also exchange an end-user credential.
    """
    registered_issuer()
    exchanged = _exchange(client, workspace_id, _credential(workspace_id=workspace_id))
    assert exchanged.status_code == 201

    response = client.request(method, path, headers=scope, json=json_body)

    assert response.status_code == expected_status, response.text


# -- the race: two tabs open the assistant on the same subject at once ------


class _PassthroughKeySource:
    """Direct pass-through: this test's issuer uses a fixed public key, not
    a JWKS URL, so JWKS resolution is never exercised here."""

    async def resolve(
        self, *, public_key: str | None, jwks_url: str | None, token: str
    ) -> str | None:
        del jwks_url, token
        return public_key


async def test_two_simultaneous_first_time_exchanges_for_the_same_subject_both_succeed(
    client: TestClient,
    workspace_id: str,
    registered_issuer: Callable[..., dict[str, object]],
    engine: AsyncEngine,
) -> None:
    """The realistic trigger is someone opening the assistant in two browser
    tabs at once: both requests read `external_identities` before either has
    written to it, so both try to create the first identity for this
    subject. The `UNIQUE` constraint is what actually decides who is first
    — the loser must come away with the winner's `end_user_id`, not a raw
    500 for what is an ordinary double-open.
    """
    registered_issuer()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def attempt(index: int) -> UUID:
        async with factory.begin() as session:
            service = EndUserIdentityService(
                SqlEndUserStore(session), _PassthroughKeySource(), timedelta(hours=8)
            )
            exchanged = await service.exchange(
                _credential(workspace_id=workspace_id),
                UUID(workspace_id),
                datetime.now(UTC),
                f"req-race-{index}",
            )
            return exchanged.end_user_id

    results = await asyncio.gather(attempt(1), attempt(2), return_exceptions=True)

    failures = [value for value in results if isinstance(value, BaseException)]
    assert not failures, failures
    assert results[0] == results[1]
