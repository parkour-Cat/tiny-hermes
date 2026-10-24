"""Which sandbox host a call goes to (tier C §4.3, §4.4).

Placement picks a host likely to admit; the host's own admission decides.
So the pool only ever moves on when it is certain nothing happened on the
host it asked: refused for room, or never connected. A call that may have
been received is the recovery flow's to sort out, not a reason to make a
second sandbox somewhere else.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from tiny_hermes.sandbox.application.controller import (
    RefusalReason,
    SandboxRefused,
    SandboxUnreachable,
)
from tiny_hermes.sandbox.domain.models import CacheState
from tiny_hermes.sandbox.transport.adapter import AcquiredSandbox
from tiny_hermes.sandbox.transport.pool import SandboxPool

RUN = uuid4()


class _Directory:
    def __init__(
        self,
        *,
        placement: list[str] | None = None,
        claims: dict[UUID, str] | None = None,
        sandboxes: dict[UUID, str] | None = None,
        urls: dict[str, str | None] | None = None,
    ) -> None:
        self._placement = placement or []
        self._claims = claims or {}
        self._sandboxes = sandboxes or {}
        self._urls = urls if urls is not None else {}
        self.sandbox_lookups = 0

    async def host_of_run(self, run_id: UUID) -> str | None:
        return self._claims.get(run_id)

    async def host_of_sandbox(self, sandbox_id: UUID) -> str | None:
        self.sandbox_lookups += 1
        return self._sandboxes.get(sandbox_id)

    async def placement(self) -> list[str]:
        return list(self._placement)

    async def url_of(self, name: str) -> str | None:
        return self._urls.get(name, f"tls://{name}.internal:7700")


class _Host:
    """One host's controller, answering acquires in a fixed way."""

    def __init__(self, name: str, answer: Exception | None = None) -> None:
        self.name = name
        self.answer = answer
        self.calls: list[str] = []
        self.sandbox_id = uuid4()

    async def acquire(self, **_: Any) -> AcquiredSandbox:
        self.calls.append("acquire")
        if self.answer is not None:
            raise self.answer
        return AcquiredSandbox(sandbox_id=self.sandbox_id, cache_state=CacheState.RESET)

    async def freeze(self, **_: Any) -> None:
        self.calls.append("freeze")


def _pool(directory: _Directory, *hosts: _Host) -> SandboxPool:
    by_url = {f"tls://{host.name}.internal:7700": host for host in hosts}
    return SandboxPool(directory, connect=lambda url: by_url[url])  # type: ignore[arg-type,return-value]


async def _acquire(pool: SandboxPool) -> AcquiredSandbox:
    return await pool.acquire(
        run_id=RUN, lease_id=uuid4(), workspace_id=uuid4(), profile="default"
    )


def _exhausted() -> SandboxRefused:
    return SandboxRefused(RefusalReason.MEMORY_BUDGET_EXHAUSTED)


async def test_a_run_with_a_sandbox_goes_back_to_its_host_only() -> None:
    """A frozen sandbox thaws only where it is. Placement would prefer the
    emptier host, and that would be a second sandbox for one Run."""
    a, b = _Host("sbx-a"), _Host("sbx-b")
    pool = _pool(_Directory(placement=["sbx-a", "sbx-b"], claims={RUN: "sbx-b"}), a, b)

    await _acquire(pool)

    assert (a.calls, b.calls) == ([], ["acquire"])


async def test_its_own_host_refusing_for_room_is_not_a_reason_to_go_elsewhere() -> None:
    a, b = _Host("sbx-a"), _Host("sbx-b", _exhausted())
    pool = _pool(_Directory(placement=["sbx-a"], claims={RUN: "sbx-b"}), a, b)

    with pytest.raises(SandboxRefused):
        await _acquire(pool)

    assert a.calls == []


async def test_a_new_sandbox_goes_to_the_first_host_placement_names() -> None:
    a, b = _Host("sbx-a"), _Host("sbx-b")
    pool = _pool(_Directory(placement=["sbx-b", "sbx-a"]), a, b)

    made = await _acquire(pool)

    assert made.sandbox_id == b.sandbox_id
    assert a.calls == []


async def test_a_host_out_of_room_passes_to_the_next() -> None:
    a, b = _Host("sbx-a", _exhausted()), _Host("sbx-b")
    pool = _pool(_Directory(placement=["sbx-a", "sbx-b"]), a, b)

    made = await _acquire(pool)

    assert made.sandbox_id == b.sandbox_id


async def test_a_host_never_connected_to_passes_to_the_next() -> None:
    a = _Host("sbx-a", SandboxUnreachable("refused", sent=False))
    b = _Host("sbx-b")
    pool = _pool(_Directory(placement=["sbx-a", "sbx-b"]), a, b)

    made = await _acquire(pool)

    assert made.sandbox_id == b.sandbox_id


async def test_a_host_that_may_have_received_the_acquire_is_not_passed_over() -> None:
    """It may have made the sandbox and failed to say so. Asking the next host
    could leave the Run with two."""
    a = _Host("sbx-a", SandboxUnreachable("closed mid-call", sent=True))
    b = _Host("sbx-b")
    pool = _pool(_Directory(placement=["sbx-a", "sbx-b"]), a, b)

    with pytest.raises(SandboxUnreachable) as unreachable:
        await _acquire(pool)

    assert unreachable.value.sent is True
    assert b.calls == []


async def test_any_other_refusal_is_the_answer() -> None:
    a = _Host("sbx-a", SandboxRefused(RefusalReason.IMAGE_NOT_APPROVED))
    b = _Host("sbx-b")
    pool = _pool(_Directory(placement=["sbx-a", "sbx-b"]), a, b)

    with pytest.raises(SandboxRefused) as refused:
        await _acquire(pool)

    assert refused.value.reason is RefusalReason.IMAGE_NOT_APPROVED
    assert b.calls == []


async def test_every_host_full_is_a_full_pool() -> None:
    """The Run waits its ten seconds and tries again (§21.3)."""
    a, b = _Host("sbx-a", _exhausted()), _Host("sbx-b", SandboxUnreachable("x", sent=False))
    pool = _pool(_Directory(placement=["sbx-a", "sbx-b"]), a, b)

    with pytest.raises(SandboxRefused) as refused:
        await _acquire(pool)

    assert refused.value.reason is RefusalReason.MEMORY_BUDGET_EXHAUSTED


async def test_every_host_unreachable_is_unreachable_and_unsent() -> None:
    a = _Host("sbx-a", SandboxUnreachable("x", sent=False))
    b = _Host("sbx-b", SandboxUnreachable("y", sent=False))
    pool = _pool(_Directory(placement=["sbx-a", "sbx-b"]), a, b)

    with pytest.raises(SandboxUnreachable) as unreachable:
        await _acquire(pool)

    assert unreachable.value.sent is False


async def test_no_host_to_place_on_is_a_full_pool() -> None:
    """Every host draining or silent: wait, rather than fail the Run."""
    pool = _pool(_Directory(placement=[]))

    with pytest.raises(SandboxRefused) as refused:
        await _acquire(pool)

    assert refused.value.reason is RefusalReason.MEMORY_BUDGET_EXHAUSTED


async def test_a_host_with_no_address_is_never_connected_to() -> None:
    """A single-host row on a socket has no url; in a pool nobody can dial it."""
    pool = _pool(_Directory(claims={RUN: "default"}, urls={"default": None}))

    with pytest.raises(SandboxUnreachable) as unreachable:
        await _acquire(pool)

    assert unreachable.value.sent is False


async def test_a_call_on_a_sandbox_goes_to_the_host_it_lives_on() -> None:
    a, b = _Host("sbx-a"), _Host("sbx-b")
    sandbox = uuid4()
    pool = _pool(_Directory(sandboxes={sandbox: "sbx-b"}), a, b)

    await pool.freeze(run_id=RUN, lease_id=uuid4(), sandbox_id=sandbox)

    assert (a.calls, b.calls) == ([], ["freeze"])


async def test_a_sandbox_the_pool_has_no_record_of_has_no_reservation() -> None:
    """The answer its host would have given, without guessing which host."""
    pool = _pool(_Directory())

    with pytest.raises(SandboxRefused) as refused:
        await pool.freeze(run_id=RUN, lease_id=uuid4(), sandbox_id=uuid4())

    assert refused.value.reason is RefusalReason.NO_RESERVATION


async def test_a_sandbox_never_moves_so_its_host_is_looked_up_once() -> None:
    """A slice makes a call per round on the same sandbox; each one paying a
    database read for an answer that cannot change would be waste."""
    b = _Host("sbx-b")
    sandbox = uuid4()
    directory = _Directory(sandboxes={sandbox: "sbx-b"})
    pool = _pool(directory, b)

    for _ in range(3):
        await pool.freeze(run_id=RUN, lease_id=uuid4(), sandbox_id=sandbox)

    assert directory.sandbox_lookups == 1


async def test_a_sandbox_just_acquired_needs_no_lookup_at_all() -> None:
    b = _Host("sbx-b")
    directory = _Directory(placement=["sbx-b"])
    pool = _pool(directory, b)

    made = await _acquire(pool)
    await pool.freeze(run_id=RUN, lease_id=uuid4(), sandbox_id=made.sandbox_id)

    assert directory.sandbox_lookups == 0

