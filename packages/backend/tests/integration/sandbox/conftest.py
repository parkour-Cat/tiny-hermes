"""A real Docker daemon, and a Controller wired to it.

These tests create containers. They are slow by the standards of this suite and
they are the only place the container policy is checked against what Docker
actually built rather than against what the platform asked for — a test that
inspects the dict we passed proves only that we can build a dict.

Every container created here carries a label, and the session fixture sweeps
anything left over. A leaked container on a developer's machine is a real cost
and this suite is the thing most likely to cause one.
"""

import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import docker
import pytest
from docker.errors import DockerException
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.sandbox.application.controller import (
    AuditEntry,
    SandboxController,
)
from tiny_hermes.sandbox.infrastructure.docker_engine import DockerEngine
from tiny_hermes.sandbox.infrastructure.sql_store import SqlSandboxStore
from tiny_hermes.sandbox.transport.address import ControllerAddress
from tiny_hermes.sandbox.transport.certs import issue, issue_ca, write_pair
from tiny_hermes.sandbox.transport.client import ControllerClient
from tiny_hermes.sandbox.transport.server import ControllerServer
from tiny_hermes.sandbox.transport.tls import Role, TlsFiles, client_context, server_context

#: Built by `deploy/sandbox/Dockerfile`. The suite builds it if it is absent
#: rather than skipping: a sandbox suite that quietly does not run is worse
#: than one that takes a minute the first time.
IMAGE_TAG = "tiny-hermes-sandbox:test"
LABEL = "tiny-hermes.test"


#: What `sockaddr_un.sun_path` holds, including its terminator. 104 on
#: macOS and the BSDs, 108 on Linux — the smaller one, because a path that
#: fits everywhere is the only kind worth creating in a shared fixture.
MAX_SOCKET_PATH = 104


def _short_temp_root() -> str:
    """A temp directory whose name leaves room for a socket inside it.

    `tempfile.gettempdir()` is `/tmp` on Linux and
    `/var/folders/<16 chars>/<20 chars>/T` on macOS — 56 characters before
    pytest adds `pytest-of-<user>/pytest-<n>/<test name>0/`. A socket under
    that is over the limit, and the failure is `OSError: AF_UNIX path too
    long` at fixture setup: the seventeen tests in this directory have never
    run on a Mac.
    """
    # A *parent* for `mkdtemp`, never a path anything is created at: the
    # directory it returns is made with a unique name and mode 0700, which is
    # what S108 is about. Named as a constant so the rule is answered once.
    shared = "/tmp"  # noqa: S108 - parent of a mkdtemp, not a fixed filename
    if os.path.isdir(shared) and os.access(shared, os.W_OK):
        return shared
    return tempfile.gettempdir()  # pragma: no cover - environment without /tmp


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """Somewhere to put a Unix socket, short enough that the kernel takes it.

    Deliberately not `tmp_path`: pytest builds that name out of the test's
    own name, so whether a socket fits would depend on how long somebody
    called their test.
    """
    created = tempfile.mkdtemp(prefix="th-sock-", dir=_short_temp_root())
    try:
        yield Path(created)
    finally:
        shutil.rmtree(created, ignore_errors=True)


@dataclass(frozen=True)
class Pki:
    """One deployment's controller certificates, issued by `certs.py` itself."""

    directory: Path

    def files(self, role: Role) -> TlsFiles:
        return TlsFiles(
            ca_file=str(self.directory / "ca.pem"),
            cert_file=str(self.directory / f"{role.value}.pem"),
            key_file=str(self.directory / f"{role.value}.key"),
        )


@pytest.fixture(scope="session")
def controller_pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    directory = tmp_path_factory.mktemp("pki")
    ca = issue_ca()
    write_pair(directory, "ca", ca)
    write_pair(
        directory,
        Role.CONTROLLER.value,
        issue(ca, role=Role.CONTROLLER, hosts=["127.0.0.1", "localhost"]),
    )
    for caller in (Role.WORKER, Role.SCHEDULER):
        write_pair(directory, caller.value, issue(ca, role=caller))
    return Pki(directory)


@pytest.fixture(params=["unix", "tls"])
def transport(request: pytest.FixtureRequest) -> str:
    """Every wire test runs over both, so TLS is the same protocol and not a
    second one that happens to pass a smaller suite."""
    return str(request.param)


async def wire(
    transport: str,
    socket_dir: Path,
    pki: Pki,
    *,
    role: Role = Role.WORKER,
    timeout: float = 120.0,
    **server_options: Any,
) -> tuple[ControllerServer, ControllerClient]:
    """A started server and a client pointed at it, over `transport`."""
    if transport == "unix":
        path = str(socket_dir / "c.sock")
        server = ControllerServer(path=path, **server_options)
        await server.start()
        return server, ControllerClient(path, timeout=timeout)
    server = ControllerServer(
        address=ControllerAddress(scheme="tls", host="127.0.0.1", port=0),
        tls=server_context(pki.files(Role.CONTROLLER)),
        **server_options,
    )
    await server.start()
    client = ControllerClient(
        ControllerAddress(scheme="tls", host="127.0.0.1", port=server.bound_port),
        tls=client_context(pki.files(role)),
        timeout=timeout,
    )
    return server, client


@pytest.fixture(scope="session")
def docker_client() -> Iterator[Any]:
    try:
        client: Any = docker.from_env()
        _: Any = client.ping()
    except DockerException as unreachable:  # pragma: no cover - environment
        pytest.skip(f"no reachable Docker daemon: {unreachable}")
    yield client
    client.close()


@pytest.fixture(scope="session")
def image_digest(docker_client: Any) -> str:
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..")
    context = os.path.abspath(os.path.join(root, "deploy", "sandbox"))
    image, _ = docker_client.images.build(
        path=context, dockerfile="Dockerfile", tag=IMAGE_TAG, rm=True
    )
    return str(image.id)


@pytest.fixture(autouse=True)
def sweep_containers(docker_client: Any) -> Iterator[None]:
    """Nothing this test created outlives it, whatever the test did.

    Written as a fixture rather than as cleanup inside each test because the
    tests that matter most here are the ones that fail partway through.
    """
    yield
    for container in docker_client.containers.list(
        all=True, filters={"label": f"{LABEL}=1"}
    ):
        with _ignoring_docker():
            container.remove(force=True)
    for volume in docker_client.volumes.list(filters={"name": "tiny-hermes-"}):
        with _ignoring_docker():
            volume.remove(force=True)


class _ignoring_docker:  # noqa: N801 - a context manager used as a statement
    def __enter__(self) -> None:
        return None

    def __exit__(self, kind: object, error: object, traceback: object) -> bool:
        return isinstance(error, DockerException)


class StubLeases:
    """Answers the one question the Controller asks about leases.

    A stub rather than the real authority, because these tests are about what
    the Controller does with the answer — it must ask, and it must refuse on
    "no". Whether the real query is right is the Run module's business and
    phase 2B's tests; `test_lease_authority.py` binds the two together.
    """

    def __init__(self) -> None:
        self._denied: set[tuple[UUID, UUID]] = set()
        self._granted: set[tuple[UUID, UUID]] = set()
        self._expired: set[UUID] = set()

    def allow(self, *, run_id: UUID, lease_id: UUID) -> None:
        self._granted.add((run_id, lease_id))

    def deny(self, *, run_id: UUID, lease_id: UUID) -> None:
        self._denied.add((run_id, lease_id))

    def expire(self, *, run_id: UUID) -> None:
        self._expired.add(run_id)

    def renew(self, *, run_id: UUID) -> None:
        """A new slice: the Run holds a live lease again."""
        self._expired.discard(run_id)

    async def holds(self, run_id: UUID, lease_id: UUID) -> bool:
        return (run_id, lease_id) not in self._denied

    async def any_live(self, run_id: UUID) -> bool:
        return run_id not in self._expired


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


@pytest.fixture
def leases() -> StubLeases:
    return StubLeases()


@pytest.fixture
def audit() -> RecordingAudit:
    return RecordingAudit()


@pytest.fixture
async def controller(
    engine: AsyncEngine,
    empty_database: None,
    docker_client: Any,
    image_digest: str,
    leases: StubLeases,
    audit: RecordingAudit,
) -> AsyncIterator[SandboxController]:
    sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )
    async with sessions() as session:
        yield SandboxController(
            engine=DockerEngine(docker_client, extra_labels={LABEL: "1"}),
            store=SqlSandboxStore(session),
            approved_digests=(image_digest,),
            leases=leases,
            audit=audit,
        )
        await session.commit()
