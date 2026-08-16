"""What the socket adapter does with an error it cannot name.

The Controller answers every exception with a string: a decided refusal
carries its `RefusalReason` value, and anything else carries the exception's
class name. The adapter has to tell those apart, because only the first one
is an answer the caller can act on.
"""

from typing import Any

import pytest
from tiny_hermes.sandbox.application.controller import RefusalReason, SandboxRefused
from tiny_hermes.sandbox.transport.adapter import ControllerSocketSandbox
from tiny_hermes.sandbox.transport.client import ControllerClient


class _RefusingClient:
    """A client whose every call is refused with one fixed string."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        del action, payload
        raise ControllerClient.Refused(self._reason)


def _sandbox(reason: str) -> ControllerSocketSandbox:
    adapter = ControllerSocketSandbox.__new__(ControllerSocketSandbox)
    adapter._client = _RefusingClient(reason)  # type: ignore[attr-defined]  # noqa: SLF001
    return adapter


async def test_a_named_refusal_arrives_as_the_platform_type() -> None:
    sandbox = _sandbox(RefusalReason.ALREADY_RESERVED.value)

    with pytest.raises(SandboxRefused) as refused:
        await sandbox._call("acquire", {})  # noqa: SLF001

    assert refused.value.reason is RefusalReason.ALREADY_RESERVED


async def test_an_unnameable_error_keeps_the_transport_error() -> None:
    """`DockerUnavailable` is an exception class, not a refusal.

    It reached the Scheduler's cleanup as `ValueError: 'DockerUnavailable' is
    not a valid RefusalReason`, once per attempt, because the adapter raised
    the enum lookup's own failure instead of the error it was handed.
    """
    sandbox = _sandbox("DockerUnavailable")

    with pytest.raises(ControllerClient.Refused) as refused:
        await sandbox._call("volume_remove", {})  # noqa: SLF001

    assert refused.value.reason == "DockerUnavailable"
