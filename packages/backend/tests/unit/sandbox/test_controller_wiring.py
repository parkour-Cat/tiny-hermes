"""Settings become a listening Controller and a calling Worker or Scheduler.

Without this, the TLS transport would be a feature no process reaches: the
setting would parse, the tests of the wire would pass, and every deployment
would still be on the Unix socket.
"""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tiny_hermes.sandbox.transport.certs import issue, issue_ca, write_pair
from tiny_hermes.sandbox.transport.tls import IdentityError, Role
from tiny_hermes.sandbox.transport.wiring import controller_client, controller_listener
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


@pytest.fixture
def pki(tmp_path: Path) -> Path:
    ca = issue_ca()
    write_pair(tmp_path, "ca", ca)
    write_pair(tmp_path, "controller", issue(ca, role=Role.CONTROLLER, hosts=["sandbox-1"]))
    write_pair(tmp_path, "worker", issue(ca, role=Role.WORKER))
    write_pair(tmp_path, "scheduler", issue(ca, role=Role.SCHEDULER))
    soon = datetime.now(UTC) - timedelta(days=350)
    write_pair(tmp_path, "worker-soon", issue(ca, role=Role.WORKER, days=360, now=soon))
    return tmp_path


def _tls(pki: Path, name: str, **overrides: Any) -> Settings:
    return _settings(
        sandbox_controller_ca_file=str(pki / "ca.pem"),
        sandbox_controller_cert_file=str(pki / f"{name}.pem"),
        sandbox_controller_key_file=str(pki / f"{name}.key"),
        **overrides,
    )


def test_a_single_host_stack_calls_over_the_socket() -> None:
    client = controller_client(_settings(), role=Role.WORKER)

    assert client.path == "/run/tiny-hermes/controller.sock"
    assert not client.address.is_tls


def test_a_worker_calls_a_tls_controller_with_its_own_certificate(pki: Path) -> None:
    settings = _tls(pki, "worker", sandbox_controller_url="tls://sandbox-1:7443")

    client = controller_client(settings, role=Role.WORKER)

    assert str(client.address) == "tls://sandbox-1:7443"


def test_a_scheduler_started_with_the_worker_certificate_does_not_start(pki: Path) -> None:
    settings = _tls(pki, "worker", sandbox_controller_url="tls://sandbox-1:7443")

    with pytest.raises(IdentityError, match="scheduler"):
        controller_client(settings, role=Role.SCHEDULER)


def test_a_single_host_controller_listens_on_the_socket() -> None:
    address, context = controller_listener(_settings())

    assert address.path == "/run/tiny-hermes/controller.sock"
    assert context is None


def test_a_tls_controller_listens_with_its_own_certificate(pki: Path) -> None:
    settings = _tls(pki, "controller", sandbox_controller_listen="tls://10.0.0.5:7443")

    address, context = controller_listener(settings)

    assert str(address) == "tls://10.0.0.5:7443"
    assert context is not None


def test_a_controller_holding_a_caller_certificate_does_not_start(pki: Path) -> None:
    settings = _tls(pki, "worker", sandbox_controller_listen="tls://10.0.0.5:7443")

    with pytest.raises(IdentityError, match="controller"):
        controller_listener(settings)


def test_a_certificate_near_its_end_is_warned_about_at_startup(
    pki: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _tls(pki, "worker-soon", sandbox_controller_url="tls://sandbox-1:7443")

    with caplog.at_level(logging.WARNING):
        controller_client(settings, role=Role.WORKER)

    assert any("expires" in record.getMessage() for record in caplog.records)
