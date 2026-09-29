import subprocess
import sys

import pytest
from tiny_hermes.api import cli


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
