"""Every parameter the platform hands Docker, decided without touching Docker.

Pure, like the address policy in phase 3A and for the same reason: the decisions
that matter are the ones about what a container may do, and they should be
settled and exhaustively tested before anything opens a socket to a daemon.

Two properties are worth naming because they are structural rather than checked:

**No bind mounts.** Both writable paths are named volumes, so a host path is not
merely forbidden — it is unrepresentable. There is no field to point anywhere,
and therefore no code path that could be talked into pointing it at `/`.

**No inherited environment.** This process holds model credentials. A sandbox
that could read them would make every other control here decorative.
"""

from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import UUID

#: The uid baked into the runtime image. Numeric rather than a name, so the
#: container's own `/etc/passwd` cannot decide who the platform meant.
SANDBOX_UID = 10001

#: Inside the container, not on this host. `S108` reads any "/tmp" as a host
#: temp path, which is the one thing this string is not: nothing in this process
#: opens it, and the container's own is a tmpfs the platform sized and mounted
#: `noexec`. Named once so the exemption is explained once.
SANDBOX_TMP = "/tmp"  # noqa: S108

DIGEST_PREFIX = "sha256:"
DIGEST_LENGTH = len(DIGEST_PREFIX) + 64


class ContainerPolicyError(Exception):
    """A container the platform will not describe, let alone create."""


class UnapprovedImage(ContainerPolicyError):
    pass


class ProfileTooLarge(ContainerPolicyError):
    pass


@dataclass(frozen=True)
class ResourceProfile:
    """The dimensions a sandbox may consume.

    There is deliberately no disk number here. The old ``disk_mb`` was
    declared and never enforced — Docker's ``storage_opt`` covers neither
    named volumes nor tmpfs — and a limit that reads like one but is not is
    worse than no field at all. Committed data answers to the checkpoint
    quota (``workspace_max_bytes``); the cache answers to the kernel below.
    """

    name: str
    cpus: float
    memory_mb: int
    pids_limit: int
    cache_mb: int
    cache_inodes: int
    tmp_mb: int

    def fields(self) -> dict[str, Any]:
        """The dimensions without the name, for building a variant."""
        return {
            "cpus": self.cpus,
            "memory_mb": self.memory_mb,
            "pids_limit": self.pids_limit,
            "cache_mb": self.cache_mb,
            "cache_inodes": self.cache_inodes,
            "tmp_mb": self.tmp_mb,
        }

    def exceeds(self, ceiling: "ResourceProfile") -> bool:
        return (
            self.cpus > ceiling.cpus
            or self.memory_mb > ceiling.memory_mb
            or self.pids_limit > ceiling.pids_limit
            or self.cache_mb > ceiling.cache_mb
            or self.cache_inodes > ceiling.cache_inodes
            or self.tmp_mb > ceiling.tmp_mb
        )


#: Technical design §11.2's M1 default, and in phase 3B the only profile there
#: is. A second one arrives with something that needs it.
DEFAULT_PROFILE = ResourceProfile(
    name="default",
    cpus=1.0,
    memory_mb=1024,
    pids_limit=128,
    cache_mb=512,
    cache_inodes=200_000,
    tmp_mb=256,
)


@dataclass(frozen=True)
class VolumeMount:
    source: str
    target: str
    kind: Literal["volume"] = "volume"


@dataclass(frozen=True)
class ContainerConfig:
    """The container, as Docker will be asked for it.

    Both writable byte ceilings a container has are kernel-enforced tmpfs
    limits (`/tmp` and `/workspace/cache`). The data volume is deliberately
    unsized at the Docker level: what may *persist* is the checkpoint quota's
    decision (design §9), and a command may exceed it temporarily while it
    runs — the post-command scan is what refuses to commit the excess.

    ``volume_labels`` is not passed to `docker run`: it is what the engine
    stamps on the explicitly created data volume, so the Scheduler can later
    enumerate ownership from labels instead of parsing a name (design §13).
    """

    image: str
    user: str
    read_only: bool
    network_mode: str
    cap_drop: tuple[str, ...]
    security_opt: tuple[str, ...]
    nano_cpus: int
    mem_limit: int
    pids_limit: int
    tmpfs: dict[str, str]
    mounts: tuple[VolumeMount, ...]
    init: bool
    labels: dict[str, str]
    volume_labels: dict[str, str]

    def as_docker_kwargs(self) -> dict[str, Any]:
        """The exact argument set, and no more.

        A literal in the test compares against this, because Docker's run
        arguments are a large surface and most of the ones not here weaken
        something: `privileged`, `devices`, `pid_mode`, `ipc_mode`, `binds`.
        """
        return {
            "image": self.image,
            "command": ["sleep", "infinity"],
            "user": self.user,
            "read_only": self.read_only,
            "network_mode": self.network_mode,
            "cap_drop": list(self.cap_drop),
            "security_opt": list(self.security_opt),
            "nano_cpus": self.nano_cpus,
            "mem_limit": self.mem_limit,
            "pids_limit": self.pids_limit,
            "tmpfs": dict(self.tmpfs),
            "mounts": [
                {"Type": mount.kind, "Source": mount.source, "Target": mount.target}
                for mount in self.mounts
            ],
            "init": self.init,
            "environment": {"HOME": "/workspace/data"},
            "working_dir": "/workspace/data",
            "labels": dict(self.labels),
            "detach": True,
            # A container that vanishes on exit cannot be inspected after a
            # failure, and the Scheduler's reclamation would have nothing to
            # find. Removal is the platform's decision, at a moment it chose.
            "auto_remove": False,
        }


def container_config(
    *,
    digest: str,
    profile: ResourceProfile,
    run_id: UUID,
    instance_id: UUID,
    workspace_id: UUID | None = None,
    session_id: UUID | None = None,
    approved_digests: tuple[str, ...],
    ceiling: ResourceProfile = DEFAULT_PROFILE,
) -> ContainerConfig:
    """Describe the one container this Run is allowed.

    ``workspace_id`` and ``session_id`` become data-volume labels. They are
    optional only until Task 9 threads them through `acquire`; a label the
    caller knows must always be stamped, because the Scheduler's enumeration
    can only see what was written.
    """
    if not _is_digest(digest) or digest not in approved_digests:
        # Fails closed on an empty allowlist, which is the default when tools
        # are not configured.
        raise UnapprovedImage(f"image is not approved: {digest[:24]}")
    if profile.exceeds(ceiling):
        raise ProfileTooLarge(f"{profile.name} exceeds the instance ceiling")

    volume_labels = {
        "tiny-hermes.run": str(run_id),
        "tiny-hermes.instance": str(instance_id),
    }
    if workspace_id is not None:
        volume_labels["tiny-hermes.workspace"] = str(workspace_id)
    if session_id is not None:
        volume_labels["tiny-hermes.session"] = str(session_id)

    return ContainerConfig(
        image=digest,
        user=f"{SANDBOX_UID}:{SANDBOX_UID}",
        read_only=True,
        network_mode="none",
        cap_drop=("ALL",),
        security_opt=("no-new-privileges:true",),
        nano_cpus=int(profile.cpus * 1_000_000_000),
        mem_limit=profile.memory_mb * 1024 * 1024,
        pids_limit=profile.pids_limit,
        tmpfs={
            # A writable directory that can execute is a place to stage a
            # binary — /tmp stays noexec.
            SANDBOX_TMP: f"rw,noexec,nosuid,nodev,size={profile.tmp_mb}m",
            # Cache is *not* noexec: it is the intended home of rebuilt
            # dependencies and their executables (design §9). Its byte and
            # inode ceilings are the kernel's, and its pages compete with
            # process memory inside the existing memory limit rather than
            # consuming host memory outside it. No name, so nothing to leak
            # or accidentally reuse: it lives and dies with this container,
            # which is what makes `cache_state=reset` honest.
            "/workspace/cache": (
                f"rw,nosuid,nodev,size={profile.cache_mb}m"
                f",nr_inodes={profile.cache_inodes}"
                f",uid={SANDBOX_UID},gid={SANDBOX_UID},mode=0700"
            ),
        },
        mounts=(
            # Data outlives the instance — in 3B for the Run's own length, and
            # in 3C for the Session's.
            VolumeMount(source=f"tiny-hermes-data-{run_id}", target="/workspace/data"),
        ),
        # Without it a command that forks and exits fills the pids ceiling with
        # zombies, and the next command in the same slice fails for no visible
        # reason.
        init=True,
        labels={
            "tiny-hermes.run": str(run_id),
            "tiny-hermes.instance": str(instance_id),
        },
        volume_labels=volume_labels,
    )


def _is_digest(value: str) -> bool:
    """A tag is a name somebody can move. A digest is the bytes.

    Two forms are real and both are accepted. A locally built image is referred
    to by its bare id, `sha256:<hex>`, which is what M1's single-machine Compose
    produces. A pulled one carries its repository, `name@sha256:<hex>`. What is
    refused either way is a tag — `sandbox:latest` names whatever was pushed
    last, which is exactly the property an approved image must not have.
    """
    _, _, digest = value.rpartition("@")
    return (
        digest.startswith(DIGEST_PREFIX)
        and len(digest) == DIGEST_LENGTH
        and all(c in "0123456789abcdef" for c in digest[len(DIGEST_PREFIX) :])
    )


def profile_named(
    name: str, *, ceiling: ResourceProfile = DEFAULT_PROFILE
) -> ResourceProfile:
    """The one profile M1 has: the instance default itself.

    With a single profile, "default" *is* the ceiling the operator configured
    — a separate copy of the shipped numbers would refuse to start the moment
    settings lowered the cache below them.
    """
    if name != DEFAULT_PROFILE.name:
        raise ProfileTooLarge(f"unknown resource profile: {name}")
    return replace(ceiling, name=DEFAULT_PROFILE.name)
