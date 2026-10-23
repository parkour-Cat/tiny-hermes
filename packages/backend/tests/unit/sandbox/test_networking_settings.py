"""How the controller reads its sandbox network settings (§1.21.4, v2.21).

Written after `_networking` itself; the record says so.
"""

import logging
from ipaddress import IPv4Network
from typing import Any

import pytest
from tiny_hermes.sandbox.cli import _networking  # pyright: ignore[reportPrivateUsage]
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
        "egress_proxy_url": "http://egress-proxy:3128",
        "sandbox_egress_proxy_container": "tiny-hermes-egress-proxy-1",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_a_pool_the_proxy_and_its_url_make_networked_sandboxes() -> None:
    networking = _networking(_settings(sandbox_network_pool="10.240.0.0/16"))

    assert networking is not None
    assert networking.pool == IPv4Network("10.240.0.0/16")
    assert networking.proxy_alias == "egress-proxy"


def test_without_a_pool_a_sandbox_has_no_network() -> None:
    assert _networking(_settings()) is None


def test_the_retired_setting_alone_warns_and_gives_no_network(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Going quietly back to the shared bridge would undo the fix."""
    with caplog.at_level(logging.WARNING):
        networking = _networking(_settings(sandbox_egress_network="tiny-hermes_sandbox-egress"))

    assert networking is None
    assert any("SANDBOX_EGRESS_NETWORK is no longer read" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("pool", ["10.240.0.0/29", "fd00::/64"])
def test_a_pool_that_cannot_hold_a_sandbox_network_stops_the_controller(pool: str) -> None:
    with pytest.raises(ValueError, match="SANDBOX_NETWORK_POOL"):
        _networking(_settings(sandbox_network_pool=pool))
