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
    SandboxGone,
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


@pytest.mark.parametrize("reason", ["ContainerGone", "ContainerNotRunning"])
async def test_a_sandbox_that_can_run_nothing_arrives_as_gone(reason: str) -> None:
    """Named so the Worker can end the slice as interrupted at once, rather
    than fail on a transport error and wait out its lease."""
    sandbox = _sandbox(reason)

    with pytest.raises(SandboxGone) as gone:
        await sandbox.volume_remove(run_id=uuid4(), sandbox_id=uuid4())

    assert gone.value.reason == reason


class _SilentClient:
    """A client that could not get an answer at all."""

    def __init__(self, failure: ControllerClient.Unreachable) -> None:
        self._failure = failure

    async def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        del action, payload
        raise self._failure


async def _acquire_against(failure: ControllerClient.Unreachable) -> SandboxUnreachable:
    sandbox = SandboxClient(cast(ControllerClient, _SilentClient(failure)))
    with pytest.raises(SandboxUnreachable) as unreachable:
        await sandbox.acquire(
            run_id=uuid4(), lease_id=uuid4(), workspace_id=uuid4(), profile="default"
        )
    return unreachable.value


async def test_an_acquire_that_never_connected_was_not_sent() -> None:
    """The Worker requeues this one: nothing can have been reserved."""
    failure = await _acquire_against(ControllerClient.NotConnected("connection refused"))

    assert failure.sent is False


async def test_an_acquire_that_connected_and_heard_nothing_may_have_been_sent() -> None:
    failure = await _acquire_against(
        ControllerClient.Unreachable("the controller closed without answering")
    )

    assert failure.sent is True


class _RecordingClient:
    """A client that answers every acquire and keeps what it was sent."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        del action
        self.sent.append(payload)
        return {"sandbox_id": str(uuid4()), "cache_state": "reset"}


async def test_an_agents_declared_size_is_sent_with_the_acquire() -> None:
    """§21.3, v2.22: the size crosses the socket or the controller never sees it."""
    client = _RecordingClient()
    sandbox = SandboxClient(cast(ControllerClient, client))

    await sandbox.acquire(
        run_id=uuid4(), lease_id=uuid4(), workspace_id=uuid4(), profile="default", memory_mb=384
    )

    assert client.sent[0]["memory_mb"] == 384


async def test_no_declared_size_sends_no_key() -> None:
    """A controller from before v2.22 reads the payload by name; one that is
    sent nothing new has nothing to misread, and sizes by its default."""
    client = _RecordingClient()
    sandbox = SandboxClient(cast(ControllerClient, client))

    await sandbox.acquire(run_id=uuid4(), lease_id=uuid4(), workspace_id=uuid4(), profile="default")

    assert "memory_mb" not in client.sent[0]
