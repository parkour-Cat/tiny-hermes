"""What the controller's socket dispatch hands its Controller on an acquire."""

from typing import Any, cast
from uuid import uuid4

from tiny_hermes.sandbox.application.controller import AcquireResult, SandboxController
from tiny_hermes.sandbox.cli import _invoke  # pyright: ignore[reportPrivateUsage]
from tiny_hermes.sandbox.domain.models import CacheState


class _Recording:
    def __init__(self) -> None:
        self.asked: dict[str, Any] = {}

    async def acquire(self, **asked: Any) -> AcquireResult:
        self.asked = asked
        return AcquireResult(sandbox_id=uuid4(), cache_state=CacheState.RESET)


def _payload(**extra: Any) -> dict[str, Any]:
    return {
        "run_id": str(uuid4()),
        "lease_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "session_id": None,
        "profile": "default",
        **extra,
    }


async def test_a_declared_size_reaches_the_controller() -> None:
    controller = _Recording()

    await _invoke(cast(SandboxController, controller), "acquire", _payload(memory_mb=384))

    assert controller.asked["memory_mb"] == 384


async def test_a_worker_from_before_v2_22_sends_none_and_gets_the_default() -> None:
    controller = _Recording()

    await _invoke(cast(SandboxController, controller), "acquire", _payload())

    assert controller.asked["memory_mb"] is None
