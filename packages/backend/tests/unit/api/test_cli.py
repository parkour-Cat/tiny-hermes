import asyncio
import subprocess
import sys
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tiny_hermes.api import cli
from tiny_hermes.shared.config import Settings


def test_cli_starts_uvicorn_on_public_container_port(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, int]] = []

    def fake_run(app: str, *, host: str, port: int) -> None:
        calls.append((app, host, port))

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    cli.main()

    assert calls == [("tiny_hermes.api.app:app", "0.0.0.0", 8000)]


def _loads_feishu_sdk(module: str) -> bool:
    # A fresh interpreter: this test session has long since imported the SDK
    # through other tests, so `sys.modules` here would answer for them.
    probe = f"import sys, {module}; print('lark_oapi' in sys.modules)"
    answer = subprocess.run(  # noqa: S603 - our own interpreter, a literal program
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    return answer.stdout.strip() == "True"


def test_the_long_connection_module_does_load_the_feishu_sdk() -> None:
    # Without this, the next test would also pass on a machine where the SDK
    # is not installed at all, and prove nothing.
    assert _loads_feishu_sdk("tiny_hermes.channels.infrastructure.feishu_long_connection")


def test_the_api_and_worker_entrypoints_do_not_load_the_feishu_sdk() -> None:
    """Only the scheduler opens a Feishu long connection.

    Measured on 2026-09-29 inside the worker container: the SDK is 10,697 of
    the 11,745 modules this entrypoint loaded and about 152 MiB of a 225 MiB
    Worker. Every Worker process paid for a socket it never opens.
    """
    assert not _loads_feishu_sdk("tiny_hermes.api.cli")


def test_the_scheduler_loads_the_feishu_sdk_before_its_event_loop_starts() -> None:
    """The SDK takes `asyncio.get_event_loop()` when it is imported.

    `lark_oapi/ws/client.py` binds a module-level loop at import time and
    later calls `run_until_complete` on it from a thread. Imported inside the
    scheduler's running loop it binds *that* loop, and the long connection dies
    with "This event loop is already running" — which is what the stack did on
    2026-09-29 when the import moved into `_long_connections`. The lifecycle
    tests did not see it: they replace the SDK's channel with a fake.
    """
    program = """
import asyncio, sys
from tiny_hermes.api import cli

seen = {}

async def scheduler_body():
    # What `_long_connections` does first, inside the running loop.
    import tiny_hermes.channels.infrastructure.feishu_long_connection  # noqa: F401
    seen["bound_to_running_loop"] = (
        sys.modules["lark_oapi.ws.client"].loop is asyncio.get_running_loop()
    )

cli._scheduler = scheduler_body
cli.configure_logging = lambda: None
cli.scheduler_main()
print(seen["bound_to_running_loop"])
"""
    answer = subprocess.run(  # noqa: S603 - our own interpreter, a literal program
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    assert answer.stdout.strip() == "False"


class _Notifier:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _worker_settings(concurrency: int) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://app:app@127.0.0.1:1/app",
        "redis_url": "redis://127.0.0.1:1/0",
        "s3_endpoint": "http://127.0.0.1:1",
        "s3_bucket": "tiny-hermes",
        "s3_access_key": "test-access-key",
        "s3_secret_key": "test-secret-key",
        "session_cookie_secret": "cookie-secret-with-at-least-32-characters",
        "bootstrap_token": "bootstrap-token-with-at-least-32-characters",
        "worker_concurrency": concurrency,
    }
    # `_env_file=None`: this machine's `.env` must not decide what the test sees.
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


async def _serve_workers(
    monkeypatch: pytest.MonkeyPatch, concurrency: int
) -> tuple[list[Any], list[_Notifier], list[dict[str, Any]]]:
    """Run `_worker()` with every outside dependency replaced, until all start.

    The runtimes are fakes that only record how they were built and wait on
    `stop`; `stop` is set once `concurrency` of them are waiting at the same
    time, so returning at all proves they ran together, not one after another.
    """
    stop = asyncio.Event()
    runtimes: list[Any] = []
    notifiers: list[_Notifier] = []
    pools: list[dict[str, Any]] = []

    class FakeRuntime:
        def __init__(self, **kwargs: Any) -> None:
            self.worker_id = kwargs["settings"].worker_id
            self.notifier = kwargs["notifier"]
            self.sessions = kwargs["session_factory"]
            runtimes.append(self)

        async def run_forever(self, stopping: asyncio.Event) -> None:
            waiting.append(self)
            if len(waiting) == concurrency:
                stop.set()
            await stopping.wait()

    waiting: list[FakeRuntime] = []

    def new_notifier(_: Settings) -> _Notifier:
        notifier = _Notifier()
        notifiers.append(notifier)
        return notifier

    def sessions_for(_: Settings, **pool: Any) -> Any:
        pools.append(pool)
        return async_sessionmaker(create_async_engine("postgresql+asyncpg://x:y@127.0.0.1:1/z"))

    async def no_bucket(_: Any) -> None:
        return None

    def nothing(_: Settings) -> None:
        return None

    monkeypatch.setattr(cli, "get_settings", lambda: _worker_settings(concurrency))
    monkeypatch.setattr(cli, "build_session_factory", sessions_for)
    monkeypatch.setattr(cli, "_notifier", new_notifier)
    monkeypatch.setattr(cli, "_workspace", nothing)
    monkeypatch.setattr(cli, "_ensure_bucket", no_bucket)
    monkeypatch.setattr(cli, "_controller", nothing)
    monkeypatch.setattr(cli, "WorkerRuntime", FakeRuntime)
    monkeypatch.setattr(cli, "_stop_on_termination", lambda: stop)

    await asyncio.wait_for(cli._worker(), timeout=10)  # pyright: ignore[reportPrivateUsage]
    return runtimes, notifiers, pools


async def test_a_worker_process_runs_as_many_workers_as_it_is_configured_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtimes, _, _ = await _serve_workers(monkeypatch, 3)

    assert len(runtimes) == 3
    # Three Workers, as far as leases and logs are concerned.
    assert len({runtime.worker_id for runtime in runtimes}) == 3
    # One database pool for the process, shared.
    assert len({id(runtime.sessions) for runtime in runtimes}) == 1


async def test_each_worker_has_its_own_wake_up_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis subscription is one connection read by one waiter.

    `RedisWakeUpNotifier.wait` reads its subscription with `get_message`;
    shared by several Workers, the one that reads a notification takes it and
    the rest sleep out their poll interval. So each Worker gets its own.
    """
    runtimes, notifiers, _ = await _serve_workers(monkeypatch, 3)

    assert len({id(runtime.notifier) for runtime in runtimes}) == 3
    assert all(notifier.closed for notifier in notifiers)


async def test_one_worker_is_still_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    runtimes, _, pools = await _serve_workers(monkeypatch, 1)

    assert len(runtimes) == 1
    # And its pool is what every Worker had before this setting existed.
    assert pools == [{"pool_size": 5, "max_overflow": 10}]


async def test_the_pool_grows_with_the_workers_sharing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Worker can hold two connections at once: its slice's and its lease
    renewal's. The engine default (5 + 10) is exhausted by eight of them."""
    _, _, pools = await _serve_workers(monkeypatch, 16)

    assert pools == [{"pool_size": 16, "max_overflow": 16}]
