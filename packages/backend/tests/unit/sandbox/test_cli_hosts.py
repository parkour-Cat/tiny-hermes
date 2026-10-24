"""What the controller registers about itself when it starts (tier C step 3)."""

import asyncio
import contextlib
from typing import Any

import pytest
from tiny_hermes.sandbox.cli import heartbeat_forever, register_this_host
from tiny_hermes.sandbox.domain.models import NetworkPoolOverlap
from tiny_hermes.shared.config import Settings


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://app:app@db/app",
        "redis_url": "redis://redis:6379/0",
        "s3_endpoint": "http://minio:9000",
        "s3_bucket": "tiny-hermes",
        "s3_access_key": "test-access-key",
        "s3_secret_key": "test-secret-key",
        "session_cookie_secret": "cookie-secret-with-at-least-32-characters",
        "bootstrap_token": "bootstrap-token-with-at-least-32-characters",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


class _Hosts:
    def __init__(self, overlap: NetworkPoolOverlap | None = None) -> None:
        self.registered: dict[str, Any] = {}
        self._overlap = overlap

    async def register(self, **told: Any) -> Any:
        if self._overlap is not None:
            raise self._overlap
        self.registered = told
        return None


async def test_it_registers_its_name_address_budget_and_pool() -> None:
    hosts = _Hosts()
    settings = _settings(
        sandbox_controller_name="sbx-a",
        sandbox_controller_advertise_url="tls://10.0.0.5:7700",
        sandbox_network_pool="10.240.0.0/16",
    )

    await register_this_host(hosts, settings, budget_mb=4002)

    assert hosts.registered == {
        "name": "sbx-a",
        "url": "tls://10.0.0.5:7700",
        "memory_budget_mb": 4002,
        "network_pool": "10.240.0.0/16",
    }


async def test_a_host_without_a_network_registers_no_pool() -> None:
    hosts = _Hosts()

    await register_this_host(hosts, _settings(), budget_mb=4002)

    assert hosts.registered["network_pool"] is None
    assert hosts.registered["url"] is None


async def test_an_overlapping_pool_stops_the_controller_naming_the_other_host() -> None:
    """Starting anyway would hand out addresses the other host may also hand
    out, and the proxy would charge one Run's traffic to another."""
    hosts = _Hosts(NetworkPoolOverlap(other="sbx-a", other_pool="10.240.0.0/16"))

    with pytest.raises(SystemExit, match="sbx-a.*10.240.0.0/16"):
        await register_this_host(
            hosts, _settings(sandbox_network_pool="10.240.0.0/16"), budget_mb=4002
        )


async def test_a_failed_heartbeat_does_not_stop_the_next() -> None:
    """A database the host briefly could not reach makes placement skip it
    for a while; it must not end the heartbeat for good."""
    beats: list[int] = []

    async def beat() -> None:
        beats.append(len(beats))
        if len(beats) == 1:
            raise RuntimeError("database unreachable")

    task = asyncio.create_task(heartbeat_forever(beat, every=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert len(beats) >= 3


def test_the_heartbeat_is_every_ten_seconds_unless_told_otherwise() -> None:
    assert _settings().sandbox_host_heartbeat_seconds == 10
