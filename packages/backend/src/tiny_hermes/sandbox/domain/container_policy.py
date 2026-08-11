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
    name: str
    cpus: float
    memory_mb: int
    pids_limit: int
    disk_mb: int
    tmp_mb: int

    def fields(self) -> dict[str, Any]:
        """The dimensions without the name, for building a variant."""
        return {
            "cpus": self.cpus,
            "memory_mb": self.memory_mb,
            "pids_limit": self.pids_limit,
            "disk_mb": self.disk_mb,
            "tmp_mb": self.tmp_mb,
        }

    def exceeds(self, ceiling: "ResourceProfile") -> bool:
        return (
            self.cpus > ceiling.cpus
            or self.memory_mb > ceiling.memory_mb
            or self.pids_limit > ceiling.pids_limit
            or self.disk_mb > ceiling.disk_mb
            or self.tmp_mb > ceiling.tmp_mb
        )


#: Technical design §11.2's M1 default, and in phase 3B the only profile there
#: is. A second one arrives with something that needs it.
DEFAULT_PROFILE = ResourceProfile(
    name="default", cpus=1.0, memory_mb=1024, pids_limit=128, disk_mb=2048, tmp_mb=256
)


@dataclass(frozen=True)
class VolumeMount:
    source: str
    target: str
    kind: Literal["volume"] = "volume"


@dataclass(frozen=True)
class ContainerConfig:
    image: str
    user: str
    read_only: bool
    network_mode: str
    cap_drop: tuple[str, ...]
    security_opt: tuple[str, ...]
    nano_cpus: int
    mem_limit: int
    pids_limit: int
    disk_mb: int
    tmpfs: dict[str, str]
    mounts: tuple[VolumeMount, ...]
    init: bool
    labels: dict[str, str]

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
            "storage_opt": {"size": f"{self.disk_mb}m"},
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
    approved_digests: tuple[str, ...],
    ceiling: ResourceProfile = DEFAULT_PROFILE,
) -> ContainerConfig:
    """Describe the one container this Run is allowed."""
    if not _is_digest(digest) or digest not in approved_digests:
        # Fails closed on an empty allowlist, which is the default when tools
        # are not configured.
        raise UnapprovedImage(f"image is not approved: {digest[:24]}")
    if profile.exceeds(ceiling):
        raise ProfileTooLarge(f"{profile.name} exceeds the instance ceiling")

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
        disk_mb=profile.disk_mb,
        # A writable directory that can execute is a place to stage a binary.
        tmpfs={SANDBOX_TMP: f"rw,noexec,nosuid,nodev,size={profile.tmp_mb}m"},
        mounts=(
            # Data outlives the instance — in 3B for the Run's own length, and
            # in 3C for the Session's. Cache lives and dies with one warm
            # instance, which is what makes `cache_state=reset` honest.
            VolumeMount(source=f"tiny-hermes-data-{run_id}", target="/workspace/data"),
            VolumeMount(source=f"tiny-hermes-cache-{instance_id}", target="/workspace/cache"),
        ),
        # Without it a command that forks and exits fills the pids ceiling with
        # zombies, and the next command in the same slice fails for no visible
        # reason.
        init=True,
        labels={
            "tiny-hermes.run": str(run_id),
            "tiny-hermes.instance": str(instance_id),
        },
    )


def _is_digest(value: str) -> bool:
    """A tag is a name somebody can move. A digest is the bytes."""
    return (
        value.startswith(DIGEST_PREFIX)
        and len(value) == DIGEST_LENGTH
        and all(c in "0123456789abcdef" for c in value[len(DIGEST_PREFIX) :])
    )


def profile_named(name: str) -> ResourceProfile:
    """The one profile phase 3B has."""
    if name != DEFAULT_PROFILE.name:
        raise ProfileTooLarge(f"unknown resource profile: {name}")
    return replace(DEFAULT_PROFILE)
