import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2 as httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.egress.infrastructure.sql_directory import SqlScopeDirectory
from tiny_hermes.shared.config import Settings

from ..egress_support import PROXY_TOKEN, running_proxy
from .test_endpoint_api import become_someone_else


@asynccontextmanager
async def model_server(
    status: int = 200,
    redirect: str | None = None,
) -> AsyncGenerator[tuple[str, list[str | None]]]:
    app = FastAPI()
    keys: list[str | None] = []

    @app.get("/v1/models")
    async def models(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        keys.append(request.headers.get("authorization"))
        # Sanitized /models response captured from DeepSeek on 2026-09-07.
        sample = json.loads(
            (Path(__file__).parent / "fixtures/deepseek-models.json").read_text(
                encoding="utf-8-sig"
            )
        )
        sample["data"].append(sample["data"][0])
        return JSONResponse(
            sample if status == 200 else {"error": "do-not-expose-provider-body"},
            status_code=status,
            headers={"Location": redirect} if redirect else None,
        )

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/v1", keys
    finally:
        server.should_exit = True
        await task


async def test_discovers_over_real_proxy_without_persisting_key_or_endpoint(
    concurrent_client: httpx.AsyncClient,
    admin_csrf: str,
    settings: Settings,
    engine: AsyncEngine,
) -> None:
    directory = SqlScopeDirectory(async_sessionmaker(engine))
    async with running_proxy(directory=directory) as proxy, model_server() as (url, keys):
        settings.egress_proxy_url = proxy.url
        settings.egress_proxy_token = PROXY_TOKEN
        response = await concurrent_client.post(
            "/api/v1/model-endpoints/discover",
            headers={"X-CSRF-Token": admin_csrf},
            json={"base_url": url, "api_key": "test-only"},
        )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "models": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"]
    }
    assert keys == ["Bearer test-only"]
    async with engine.connect() as db:
        for query in (
            "SELECT count(*) FROM secrets",
            "SELECT count(*) FROM model_endpoints",
            "SELECT count(*) FROM outbound_scopes",
            "SELECT count(*) FROM model_discovery_grants",
        ):
            assert await db.scalar(text(query)) == 0


async def test_discovery_grant_expires_and_does_not_open_normal_requests(
    empty_database: None,
    settings: Settings,
    engine: AsyncEngine,
) -> None:
    from tiny_hermes.api.resources import ApplicationResources
    from tiny_hermes.outbound.errors import OutboundRefused

    resources = ApplicationResources(settings)
    directory = SqlScopeDirectory(async_sessionmaker(engine))
    async with running_proxy(directory=directory) as proxy, model_server() as (url, keys):
        settings.egress_proxy_url = proxy.url
        settings.egress_proxy_token = PROXY_TOKEN
        async with resources.model_discovery_client("127.0.0.1") as allowed:
            assert (await allowed.request("GET", url + "/models")).status_code == 200
            async with resources.outbound_client() as ordinary:
                with pytest.raises(OutboundRefused):
                    await ordinary.request("GET", url + "/models")
            # A different host is refused even though it resolves to the same IP.
            with pytest.raises(OutboundRefused):
                await allowed.request("GET", url.replace("127.0.0.1", "localhost") + "/models")
            async with engine.begin() as db:
                await db.execute(
                    text(
                        "UPDATE model_discovery_grants SET expires_at = now() - interval '1 second'"
                    )
                )
            with pytest.raises(OutboundRefused):
                await allowed.request("GET", url + "/models")
        assert len(keys) == 1
    async with engine.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM model_discovery_grants")) == 0


async def test_discovery_still_refuses_loopback_with_the_production_address_policy(
    empty_database: None,
    settings: Settings,
    engine: AsyncEngine,
) -> None:
    from tiny_hermes.api.resources import ApplicationResources
    from tiny_hermes.outbound.domain.address_policy import verdict
    from tiny_hermes.outbound.errors import OutboundRefused

    resources = ApplicationResources(settings)
    async with running_proxy(
        directory=SqlScopeDirectory(async_sessionmaker(engine)), approved=(), policy=verdict
    ) as proxy:
        settings.egress_proxy_url = proxy.url
        settings.egress_proxy_token = PROXY_TOKEN
        async with resources.model_discovery_client("127.0.0.1") as client:
            with pytest.raises(OutboundRefused):
                await client.request("GET", "http://127.0.0.1/models")


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_discovery_returns_safe_actionable_errors(
    concurrent_client: httpx.AsyncClient,
    admin_csrf: str,
    settings: Settings,
    engine: AsyncEngine,
    status: int,
) -> None:
    async with running_proxy(directory=SqlScopeDirectory(async_sessionmaker(engine))) as proxy:
        settings.egress_proxy_url = proxy.url
        settings.egress_proxy_token = PROXY_TOKEN
        async with model_server(status) as (url, _):
            response = await concurrent_client.post(
                "/api/v1/model-endpoints/discover",
                headers={"X-CSRF-Token": admin_csrf},
                json={"base_url": url, "api_key": "test-only"},
            )
    assert response.status_code == 422
    assert response.json()["code"] == (
        "model_discovery_unauthorized"
        if status in (401, 403)
        else "model_discovery_unsupported"
        if status == 404
        else "model_discovery_failed"
    )
    assert "test-only" not in response.text
    assert "do-not-expose-provider-body" not in response.text


async def test_discovery_requires_platform_authority_and_csrf(
    client: TestClient,
    admin_csrf: str,
    engine: AsyncEngine,
) -> None:
    payload = {"base_url": "https://models.example.com/v1", "api_key": "test-only"}
    assert client.post("/api/v1/model-endpoints/discover", json=payload).status_code == 403
    other_csrf = await become_someone_else(client, engine)
    assert (
        client.post(
            "/api/v1/model-endpoints/discover", json=payload, headers={"X-CSRF-Token": other_csrf}
        ).status_code
        == 403
    )


async def test_discovery_does_not_follow_a_redirect_or_send_the_key_to_it(
    concurrent_client: httpx.AsyncClient,
    admin_csrf: str,
    settings: Settings,
    engine: AsyncEngine,
) -> None:
    async with running_proxy(directory=SqlScopeDirectory(async_sessionmaker(engine))) as proxy:
        settings.egress_proxy_url = proxy.url
        settings.egress_proxy_token = PROXY_TOKEN
        async with model_server() as (target, target_keys):
            async with model_server(302, target + "/models") as (url, _):
                response = await concurrent_client.post(
                    "/api/v1/model-endpoints/discover",
                    headers={"X-CSRF-Token": admin_csrf},
                    json={"base_url": url, "api_key": "test-only"},
                )
            assert response.status_code == 422
            assert target_keys == []


def test_invalid_discovery_input_does_not_echo_its_key(client: TestClient, admin_csrf: str) -> None:
    response = client.post(
        "/api/v1/model-endpoints/discover",
        headers={"X-CSRF-Token": admin_csrf},
        json={"base_url": "bad-url", "api_key": "test-sensitive-value"},
    )
    assert response.status_code == 422
    assert "test-sensitive-value" not in response.text


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.com/v1",
        "https://example.com/v1?key=test-only",
        "file:///tmp/key",
    ],
)
def test_discovery_rejects_urls_with_credentials_or_unsupported_schemes(
    client: TestClient,
    admin_csrf: str,
    url: str,
) -> None:
    response = client.post(
        "/api/v1/model-endpoints/discover",
        headers={"X-CSRF-Token": admin_csrf},
        json={"base_url": url, "api_key": "test-only"},
    )
    assert response.status_code == 422
