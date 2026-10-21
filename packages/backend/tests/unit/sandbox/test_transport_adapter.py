"""What the socket adapter does with an error it cannot name.

The Controller answers every exception with a string: a decided refusal
carries its `RefusalReason` value, and anything else carries the exception's
class name. The adapter has to tell those apart, because only the first one
is an answer the caller can act on.
"""

from typing import Any, cast
from uuid import uuid4

import pytest
from tiny_hermes.sandbox.application.controller import (
    RefusalReason,
    SandboxRefused,
    SandboxUnreachable,
)
from tiny_hermes.sandbox.transport.adapter import SandboxClient
from tiny_hermes.sandbox.transport.client import ControllerClient


class _RefusingClient:
    """A client whose every call is refused with one fixed string."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        del action, payload
        raise ControllerClient.Refused(self._reason)


def _sandbox(reason: str) -> SandboxClient:
    return SandboxClient(cast(ControllerClient, _RefusingClient(reason)))


async def test_a_named_refusal_arrives_as_the_platform_type() -> None:
    sandbox = _sandbox(RefusalReason.ALREADY_RESERVED.value)

    with pytest.raises(SandboxRefused) as refused:
        await sandbox.volume_remove(run_id=uuid4(), sandbox_id=uuid4())

    assert refused.value.reason is RefusalReason.ALREADY_RESERVED


async def test_an_unnameable_error_keeps_the_transport_error() -> None:
    """`DockerUnavailable` is an exception class, not a refusal.

    It reached the Scheduler's cleanup as `ValueError: 'DockerUnavailable' is
    not a valid RefusalReason`, once per attempt, because the adapter raised
    the enum lookup's own failure instead of the error it was handed.
    """
    sandbox = _sandbox("DockerUnavailable")

    with pytest.raises(ControllerClient.Refused) as refused:
        await sandbox.volume_remove(run_id=uuid4(), sandbox_id=uuid4())

    assert refused.value.reason == "DockerUnavailable"


class _SilentClient:
    """A client that could not get an answer at all."""

    async def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        del action, payload
        raise ControllerClient.Unreachable("the controller closed without answering")


async def test_an_acquire_nobody_answered_arrives_as_the_platform_type() -> None:
    """The Worker waits on this rather than failing the Run, and it should not
    have to import the socket client to recognise it."""
    sandbox = SandboxClient(cast(ControllerClient, _SilentClient()))

    with pytest.raises(SandboxUnreachable):
        await sandbox.acquire(
            run_id=uuid4(), lease_id=uuid4(), workspace_id=uuid4(), profile="default"
        )
