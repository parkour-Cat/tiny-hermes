"""What the controller admits against, worked out once when it starts."""

from pathlib import Path
from typing import Any

import pytest
from tiny_hermes.sandbox.cli import memory_budget_mb, resource_ceiling
from tiny_hermes.shared.config import Settings

#: The first line of /proc/meminfo on the 8 GiB OrbStack VM the budget was
#: sized on.
MEMINFO = "MemTotal:        8196880 kB\nMemFree:          616000 kB\n"


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


def _meminfo(tmp_path: Path, text: str = MEMINFO) -> Path:
    path = tmp_path / "meminfo"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_ceiling_carries_the_configured_memory_limit() -> None:
    ceiling = resource_ceiling(_settings(sandbox_memory_mb=512, sandbox_cache_mb=256))

    assert ceiling.memory_mb == 512
    assert ceiling.cache_mb == 256


def test_a_configured_budget_is_used_as_it_is(tmp_path: Path) -> None:
    budget = memory_budget_mb(_settings(sandbox_memory_budget_mb=3_000), meminfo=_meminfo(tmp_path))

    assert budget == 3_000


def test_without_one_the_budget_is_half_the_hosts_memory(tmp_path: Path) -> None:
    assert memory_budget_mb(_settings(), meminfo=_meminfo(tmp_path)) == 8196880 // 1024 // 2


def test_a_host_too_small_for_one_sandbox_stops_the_controller_with_the_way_out(
    tmp_path: Path,
) -> None:
    """Starting would refuse every tool, forever, without saying why."""
    small = _meminfo(tmp_path, "MemTotal:        1500000 kB\n")

    with pytest.raises(ValueError, match="SANDBOX_MEMORY_BUDGET_MB"):
        memory_budget_mb(_settings(sandbox_memory_mb=1_024), meminfo=small)
