"""The OIDC login flow (design §2) end to end: a real egress proxy, a real
stand-in IdP on a real socket, and the actual FastAPI app — the same
technique `test_jwks_fetch.py` and `test_tarball_import.py` use to prove a
feature crosses the egress boundary rather than some other path.

This is a stub IdP, not a real one. §5 of the plan asks the record to say
what an e2e run did *not* prove — see this session's own report: nothing
here has been checked against a real vendor (Google, Okta, Auth0, ...), only
against the OIDC Core wire shapes this stand-in implements by hand.

Every call that makes the API reach *outward* — `/start` and `/callback`, the
two routes that fetch discovery/JWKS or hit the token endpoint — goes through
`_outward`, not a bare `app_client.get`. `TestClient` blocks the calling
thread, and the stand-in IdP and the proxy both live in this test's own event
loop (`test_mcp_tools.py::_post` names the same trap): a request issued
straight from the test body would hold the loop that has to answer it, and
the whole test would hang rather than fail.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.api.app import create_app
from tiny_hermes.shared.config import Settings

from ..egress_support import PROXY_TOKEN, ProxyHandle, running_proxy

BOOTSTRAP_TOKEN = "a" * 32
PASSWORD = "long-pass-123"  # noqa: S105 - fixed local test credential
CLIENT_ID = "platform-console"
CLIENT_SECRET_ENV = "OIDC_INTEGRATION_TEST_SECRET"  # noqa: S105 - an env var name


def _rsa_keypair() -> tuple[Any, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, public_pem


PRIVATE_KEY, PUBLIC_PEM = _rsa_keypair()
_OTHER_PRIVATE_KEY, _ = _rsa_keypair()
JWK = json.loads(RSAAlgorithm(RSAAlgorithm.SHA256).to_jwk(PRIVATE_KEY.public_key()))
JWK["kid"] = "stand-in-2026"


@dataclass
class StandInIdp:
    """A fake IdP: discovery, JWKS, and a token endpoint whose `id_token` a
    test sets right before the callback that will consume it — the test
    itself signs tokens, this just hands back whichever one is queued."""

    issuer: str = ""
    next_id_token: str | None = None
    next_token_status: int = 200
    token_requests: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        path = str(scope["path"])
        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        status, payload = self._answer(path, bytes(body))
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    def _answer(self, path: str, body: bytes) -> tuple[int, bytes]:
        if path == "/.well-known/openid-configuration":
            return 200, json.dumps(
                {
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{self.issuer}/authorize",
                    "token_endpoint": f"{self.issuer}/token",
                    "jwks_uri": f"{self.issuer}/jwks.json",
                }
            ).encode()
        if path == "/jwks.json":
            return 200, json.dumps({"keys": [JWK]}).encode()
        if path == "/token":
            parsed = {key: values[0] for key, values in parse_qs(body.decode()).items()}
            self.token_requests.append(parsed)
            if self.next_token_status != 200 or self.next_id_token is None:
                return self.next_token_status, b'{"error":"invalid_grant"}'
            return 200, json.dumps({"id_token": self.next_id_token}).encode()
        return 404, b"{}"


async def _serving(app: StandInIdp) -> AsyncIterator[str]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(1_000):
            if server.started:
                break
            await asyncio.sleep(0.01)
        address: Any = server.servers[0].sockets[0].getsockname()
        yield f"http://127.0.0.1:{int(address[1])}"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def idp() -> AsyncIterator[StandInIdp]:
    app = StandInIdp()
    async for url in _serving(app):
        app.issuer = url
        yield app


@pytest.fixture
async def proxy() -> AsyncIterator[ProxyHandle]:
    async with running_proxy() as handle:
        yield handle


def _id_token(
    *, sub: str, nonce: str, key: Any = PRIVATE_KEY, issuer: str, **overrides: object
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": CLIENT_ID,
        "sub": sub,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "nonce": nonce,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "stand-in-2026"})


def _state_and_nonce(location: str) -> tuple[str, str]:
    query = parse_qs(urlsplit(location).query)
    return query["state"][0], query["nonce"][0]


@pytest.fixture
async def empty_database(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE audit_events, auth_sessions, auth_identities, users, "
                "oidc_login_states, oidc_providers, memberships, workspaces CASCADE"
            )
        )


@pytest.fixture
async def app_client(
    empty_database: None, database_url: str, redis_url: str, proxy: ProxyHandle
) -> AsyncIterator[TestClient]:
    del empty_database
    settings = Settings(
        database_url=database_url,
        redis_url=redis_url,
        s3_endpoint="http://localhost:9000",
        s3_bucket="tiny-hermes-test",
        s3_access_key="tiny-hermes-local",
        s3_secret_key="tiny-hermes-local-password",
        session_cookie_secret="test-cookie-secret-with-32-characters",
        bootstrap_token=BOOTSTRAP_TOKEN,
        tiny_hermes_kek="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        egress_proxy_url=proxy.url,
        egress_proxy_token=PROXY_TOKEN,
    )
    # `with`, not a bare construction: this runs the app's lifespan and pins
    # the TestClient's own event loop, the same thing `conftest.py`'s own
    # `client` fixture does — skipping it put the app's database engine and
    # this suite's async fixtures on two different loops.
    with TestClient(create_app(settings=settings)) as value:
        yield value


@pytest.fixture
def admin_csrf(app_client: TestClient) -> str:
    created = app_client.post(
        "/api/v1/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"subject": "admin@example.com", "display_name": "Admin", "password": PASSWORD},
    )
    assert created.status_code == 201
    login = app_client.post(
        "/api/v1/auth/sessions",
        json={"subject": "admin@example.com", "password": PASSWORD},
    )
    assert login.status_code == 201
    return login.cookies["tiny_hermes_csrf"]


@pytest.fixture
def registered_provider(
    app_client: TestClient, admin_csrf: str, idp: StandInIdp, monkeypatch: pytest.MonkeyPatch
) -> str:
    monkeypatch.setenv(CLIENT_SECRET_ENV, "shhh-its-a-secret")
    created = app_client.post(
        "/api/v1/oidc/providers",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "issuer": idp.issuer,
            "client_id": CLIENT_ID,
            "client_secret_ref": CLIENT_SECRET_ENV,
            "discovery_url": f"{idp.issuer}/.well-known/openid-configuration",
            "scopes": ["openid", "email"],
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _outward(app_client: TestClient, path: str, **kwargs: Any) -> Any:
    """A GET that makes the API reach the stand-in IdP, without deadlocking
    the loop that has to run the stand-in and the proxy. See the module
    docstring."""
    return await asyncio.to_thread(lambda: app_client.get(path, **kwargs))


async def _start(app_client: TestClient, provider_id: str) -> tuple[str, str]:
    started = await _outward(
        app_client, f"/api/v1/auth/oidc/{provider_id}/start", follow_redirects=False
    )
    assert started.status_code == 302
    return _state_and_nonce(started.headers["location"])


async def _callback(app_client: TestClient, provider_id: str, *, code: str, state: str) -> Any:
    return await _outward(
        app_client,
        f"/api/v1/auth/oidc/{provider_id}/callback",
        params={"code": code, "state": state},
    )


class TestHappyPath:
    async def test_a_new_user_is_created_on_first_login_and_reused_on_a_second(
        self, app_client: TestClient, idp: StandInIdp, registered_provider: str
    ) -> None:
        state, nonce = await _start(app_client, registered_provider)
        idp.next_id_token = _id_token(sub="idp-user-1", nonce=nonce, issuer=idp.issuer)

        callback = await _callback(
            app_client, registered_provider, code="auth-code-1", state=state
        )

        assert callback.status_code == 200, callback.text
        assert callback.json()["subject"] == "idp-user-1"
        assert app_client.cookies.get("tiny_hermes_session")
        first_user_id = callback.json()["id"]

        # The same session cookie local login issues, through the same code
        # path (design §2) — proven by actually re-authenticating with it,
        # which is exactly what the `find_session` fix in `sql_store.py`
        # makes possible for an OIDC-minted session.
        me = app_client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["id"] == first_user_id

        app_client.cookies.clear()
        state2, nonce2 = await _start(app_client, registered_provider)
        idp.next_id_token = _id_token(sub="idp-user-1", nonce=nonce2, issuer=idp.issuer)
        second = await _callback(
            app_client, registered_provider, code="auth-code-2", state=state2
        )
        assert second.status_code == 200
        assert second.json()["id"] == first_user_id


async def test_the_red_line_an_oidc_login_never_links_to_a_local_user_by_email(
    app_client: TestClient, idp: StandInIdp, registered_provider: str
) -> None:
    """The one thing this whole feature must never do. A local user already
    owns `alice@example.com`; an OIDC login whose `email` claim is the exact
    same address must still land on a *different* user id."""
    login = app_client.post(
        "/api/v1/auth/sessions", json={"subject": "admin@example.com", "password": PASSWORD}
    )
    assert login.status_code == 201
    local_user_id = login.json()["id"]

    state, nonce = await _start(app_client, registered_provider)
    idp.next_id_token = _id_token(
        sub="idp-alice-unrelated-to-the-local-account",
        nonce=nonce,
        issuer=idp.issuer,
        email="admin@example.com",
    )

    callback = await _callback(app_client, registered_provider, code="auth-code", state=state)

    assert callback.status_code == 200, callback.text
    assert callback.json()["id"] != local_user_id


async def test_an_oidc_created_user_is_denied_every_workspace_resource(
    app_client: TestClient, admin_csrf: str, idp: StandInIdp, registered_provider: str
) -> None:
    """OIDC login design §3: authentication, not authorization. A JIT-created
    user has no `Membership` and reaches no workspace."""
    workspace = app_client.post(
        "/api/v1/workspaces", headers={"X-CSRF-Token": admin_csrf}, json={"name": "Primary"}
    )
    assert workspace.status_code == 201
    workspace_id = workspace.json()["id"]
    app_client.cookies.clear()

    state, nonce = await _start(app_client, registered_provider)
    idp.next_id_token = _id_token(sub="idp-user-no-membership", nonce=nonce, issuer=idp.issuer)
    callback = await _callback(app_client, registered_provider, code="auth-code", state=state)
    assert callback.status_code == 200

    denied = app_client.get("/api/v1/secrets", headers={"X-Workspace-Id": workspace_id})
    assert denied.status_code == 403


class TestFailures:
    async def test_a_disabled_provider_refuses_a_new_start(
        self, app_client: TestClient, admin_csrf: str, registered_provider: str
    ) -> None:
        disabled = app_client.post(
            f"/api/v1/oidc/providers/{registered_provider}/disable",
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert disabled.status_code == 200
        app_client.cookies.clear()

        started = await _outward(
            app_client, f"/api/v1/auth/oidc/{registered_provider}/start", follow_redirects=False
        )
        assert started.status_code == 404
        assert started.json()["code"] == "oidc_provider_not_available"

    async def test_an_unknown_state_is_refused(
        self, app_client: TestClient, registered_provider: str
    ) -> None:
        callback = await _callback(
            app_client, registered_provider, code="auth-code", state="a-state-nobody-issued"
        )
        assert callback.status_code == 400
        assert callback.json()["code"] == "oidc_login_failed"

    async def test_a_replayed_state_is_refused_the_second_time(
        self, app_client: TestClient, idp: StandInIdp, registered_provider: str
    ) -> None:
        state, nonce = await _start(app_client, registered_provider)
        idp.next_id_token = _id_token(sub="idp-user-2", nonce=nonce, issuer=idp.issuer)
        first = await _callback(app_client, registered_provider, code="auth-code", state=state)
        assert first.status_code == 200

        replay = await _callback(app_client, registered_provider, code="auth-code", state=state)
        assert replay.status_code == 400

    async def test_a_bad_signature_is_refused(
        self, app_client: TestClient, idp: StandInIdp, registered_provider: str
    ) -> None:
        state, nonce = await _start(app_client, registered_provider)
        idp.next_id_token = _id_token(
            sub="idp-user-3", nonce=nonce, issuer=idp.issuer, key=_OTHER_PRIVATE_KEY
        )

        callback = await _callback(app_client, registered_provider, code="auth-code", state=state)

        assert callback.status_code == 400

    async def test_an_expired_id_token_is_refused(
        self, app_client: TestClient, idp: StandInIdp, registered_provider: str
    ) -> None:
        state, nonce = await _start(app_client, registered_provider)
        now = datetime.now(UTC)
        idp.next_id_token = _id_token(
            sub="idp-user-4",
            nonce=nonce,
            issuer=idp.issuer,
            iat=int((now - timedelta(minutes=20)).timestamp()),
            exp=int((now - timedelta(minutes=10)).timestamp()),
        )

        callback = await _callback(app_client, registered_provider, code="auth-code", state=state)

        assert callback.status_code == 400

    async def test_a_bad_nonce_is_refused(
        self, app_client: TestClient, idp: StandInIdp, registered_provider: str
    ) -> None:
        state, _nonce = await _start(app_client, registered_provider)
        idp.next_id_token = _id_token(
            sub="idp-user-5", nonce="a-nonce-this-login-never-issued", issuer=idp.issuer
        )

        callback = await _callback(app_client, registered_provider, code="auth-code", state=state)

        assert callback.status_code == 400

    async def test_a_wrong_audience_is_refused(
        self, app_client: TestClient, idp: StandInIdp, registered_provider: str
    ) -> None:
        state, nonce = await _start(app_client, registered_provider)
        idp.next_id_token = _id_token(
            sub="idp-user-6", nonce=nonce, issuer=idp.issuer, aud="somebody-elses-client-id"
        )

        callback = await _callback(app_client, registered_provider, code="auth-code", state=state)

        assert callback.status_code == 400

    async def test_the_token_endpoint_refusing_the_exchange_is_refused(
        self, app_client: TestClient, idp: StandInIdp, registered_provider: str
    ) -> None:
        state, _nonce = await _start(app_client, registered_provider)
        idp.next_token_status = 400

        callback = await _callback(app_client, registered_provider, code="auth-code", state=state)

        assert callback.status_code == 400


async def test_the_login_page_can_list_providers_without_being_signed_in(
    app_client: TestClient, registered_provider: str, admin_csrf: str
) -> None:
    """The chicken-and-egg §4 runs into: a login page has to know which
    identity providers exist *before* anyone has signed in, and
    `/api/v1/oidc/providers` is admin-only by design.

    So there is a second, deliberately thin door. It carries only what a
    button needs — an id to start the flow and the issuer to label it —
    and never `client_id`, `client_secret_ref`, `discovery_url` or
    `created_by`. It does reveal which IdPs this deployment trusts, which
    is unavoidable: a login page cannot offer a choice it refuses to name.
    """
    listed = app_client.get("/api/v1/auth/oidc/available")

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [entry["id"] for entry in body] == [registered_provider]
    assert set(body[0]) == {"id", "issuer"}
    assert "client_secret_ref" not in listed.text
    assert "shhh-its-a-secret" not in listed.text


async def test_a_disabled_provider_is_not_offered_on_the_login_page(
    app_client: TestClient, registered_provider: str, admin_csrf: str
) -> None:
    """§1's own requirement, at the surface a person actually sees. A
    provider that can no longer complete a login must not be offered as a
    way to start one — a button that always fails is worse than no button,
    because the person cannot tell it apart from their own mistake."""
    disabled = app_client.post(
        f"/api/v1/oidc/providers/{registered_provider}/disable",
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert disabled.status_code == 200, disabled.text

    listed = app_client.get("/api/v1/auth/oidc/available")

    assert listed.status_code == 200, listed.text
    assert listed.json() == []
