"""Every parameter the platform hands Docker, decided without touching Docker.

The same shape phase 3A used for the address policy: the security-relevant
decisions are a pure function, settled and exhaustively tested before anything
opens a socket. A test here that fails is a container that would have been built
wrong; a test in `test_controller.py` that fails is a container that *was*.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from tiny_hermes.sandbox.domain.container_policy import (
    DEFAULT_PROFILE,
    SANDBOX_TMP,
    ProfileTooLarge,
    ResourceProfile,
    UnapprovedImage,
    container_config,
)

DIGEST = "sha256:" + "b" * 64
OTHER = "sha256:" + "c" * 64
RUN = UUID("11111111-2222-4333-8444-555555555555")
INSTANCE = UUID("66666666-7777-4888-8999-aaaaaaaaaaaa")


def config(**overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "digest": DIGEST,
        "profile": DEFAULT_PROFILE,
        "run_id": RUN,
        "instance_id": INSTANCE,
        "approved_digests": (DIGEST,),
        "ceiling": DEFAULT_PROFILE,
    }
    fields.update(overrides)
    return container_config(**fields)


def test_the_container_runs_as_someone_who_is_not_root() -> None:
    assert config().user == "10001:10001"


def test_the_root_filesystem_is_not_writable() -> None:
    assert config().read_only is True


def test_the_container_has_no_network() -> None:
    """Stronger than a policy that says no.

    A tool that ignored every rule in the platform still cannot reach anything,
    because there is no interface to reach it through.
    """
    assert config().network_mode == "none"


def test_every_capability_is_dropped() -> None:
    assert config().cap_drop == ("ALL",)


def test_privileges_cannot_be_gained() -> None:
    assert "no-new-privileges:true" in config().security_opt


def test_the_ceilings_come_from_the_profile() -> None:
    answer = config()
    assert answer.nano_cpus == 1_000_000_000
    assert answer.mem_limit == 1024 * 1024 * 1024
    assert answer.pids_limit == 128


def test_tmp_is_a_tmpfs_that_cannot_execute() -> None:
    """A writable directory that can execute is a place to stage a binary."""
    options = config().tmpfs[SANDBOX_TMP]
    assert "noexec" in options
    assert "nosuid" in options
    assert "nodev" in options
    assert "size=256m" in options


def test_an_init_process_reaps_what_a_command_leaves_behind() -> None:
    """Without it, a command that forks and exits fills the pids ceiling with
    zombies and the next command in the same slice fails for no visible reason."""
    assert config().init is True


def test_exactly_two_volumes_are_mounted() -> None:
    mounts = config().mounts
    assert {mount.target for mount in mounts} == {"/workspace/data", "/workspace/cache"}


def test_no_mount_is_a_host_path() -> None:
    """Named volumes only, so a host path cannot appear in the configuration.

    §6.4 forbids storing one; this goes further and makes one unrepresentable —
    there is no bind mount to point anywhere, so no code path exists that could
    be talked into pointing it at `/`.
    """
    for mount in config().mounts:
        assert mount.kind == "volume"
        assert "/" not in mount.source
        assert not mount.source.startswith(("C:", "\\\\", "."))


def test_the_data_volume_belongs_to_the_run_and_the_cache_to_the_instance() -> None:
    """Technical design §11.3.

    Cache lives and dies with one warm instance. Data outlives it — in 3B only
    for the Run's own length, and in 3C for the Session's.
    """
    by_target = {mount.target: mount.source for mount in config().mounts}
    assert str(RUN) in by_target["/workspace/data"]
    assert str(INSTANCE) in by_target["/workspace/cache"]


def test_two_runs_never_share_a_writable_layer() -> None:
    first = {m.target: m.source for m in config().mounts}
    second = {m.target: m.source for m in config(run_id=uuid4(), instance_id=uuid4()).mounts}
    assert first["/workspace/data"] != second["/workspace/data"]
    assert first["/workspace/cache"] != second["/workspace/cache"]


def test_an_image_outside_the_approved_list_is_refused() -> None:
    """§11.3: the Controller accepts no Agent-specified image.

    The allowlist is the only source, so an endpoint that could name an image
    would be an endpoint that could run anything the daemon can pull.
    """
    with pytest.raises(UnapprovedImage):
        config(digest=OTHER)


def test_a_tag_is_refused_even_when_it_is_the_approved_image() -> None:
    """A tag is a name somebody can move. A digest is the bytes."""
    with pytest.raises(UnapprovedImage):
        config(
            digest="tiny-hermes-sandbox:latest",
            approved_digests=("tiny-hermes-sandbox:latest",),
        )


def test_a_repository_qualified_digest_is_accepted() -> None:
    """The form a pulled image has.

    A locally built one is a bare `sha256:<hex>`, which is what M1's Compose
    produces; a registry one carries its repository. Both are digests, and the
    difference between them is not a security property.
    """
    pulled = f"registry.example.com/tiny-hermes/sandbox@{DIGEST}"
    assert config(digest=pulled, approved_digests=(pulled,)).image == pulled


def test_a_repository_with_a_tag_is_still_refused() -> None:
    tagged = "registry.example.com/tiny-hermes/sandbox:v1"
    with pytest.raises(UnapprovedImage):
        config(digest=tagged, approved_digests=(tagged,))


def test_an_empty_allowlist_approves_nothing() -> None:
    """The default when tools are not configured. It must fail closed."""
    with pytest.raises(UnapprovedImage):
        config(approved_digests=())


@pytest.mark.parametrize(
    "oversized",
    [
        {"cpus": 2.0},
        {"memory_mb": 2048},
        {"pids_limit": 256},
        {"disk_mb": 4096},
        {"tmp_mb": 512},
    ],
)
def test_a_profile_above_the_instance_ceiling_is_refused(oversized: dict[str, Any]) -> None:
    """An AgentVersion may select a profile no larger than the ceiling.

    3B has one profile, so this compares a value with itself — written anyway,
    because the second profile must not be the moment the rule is invented.
    """
    with pytest.raises(ProfileTooLarge):
        config(profile=ResourceProfile(name="big", **{**DEFAULT_PROFILE.fields(), **oversized}))


def test_a_profile_below_the_ceiling_is_allowed() -> None:
    smaller = ResourceProfile(name="small", **{**DEFAULT_PROFILE.fields(), "memory_mb": 512})
    assert config(profile=smaller).mem_limit == 512 * 1024 * 1024


def test_the_docker_arguments_are_exactly_these_and_no_others() -> None:
    """A literal key set, so a new parameter has to be added here deliberately.

    Docker's run arguments are a large surface and most of them weaken something.
    Comparing against a literal means `privileged`, `devices`, `pid_mode`,
    `ipc_mode`, `binds` and their friends cannot arrive quietly.
    """
    assert set(config().as_docker_kwargs()) == {
        "image",
        "command",
        "user",
        "read_only",
        "network_mode",
        "cap_drop",
        "security_opt",
        "nano_cpus",
        "mem_limit",
        "pids_limit",
        "tmpfs",
        "mounts",
        "init",
        "environment",
        "working_dir",
        "labels",
        "detach",
        "auto_remove",
    }


def test_the_disk_ceiling_is_declared_but_not_enforced() -> None:
    """A named gap, pinned so it is not mistaken for a working limit.

    §11.2 lists a 2 GiB writable disk. `storage_opt` is the obvious way to
    spend it and is wrong twice: Docker accepts it only on overlay-over-xfs
    with `pquota`, and it limits the container writable layer — which, with a
    read-only root, holds almost nothing. Every byte a command writes lands in
    a named volume or the tmpfs, and `storage_opt` covers neither.

    The tmpfs is sized. The volumes are not. This test fails the moment
    somebody adds a disk argument, which is when the enforcement story has to
    be written properly.
    """
    assert DEFAULT_PROFILE.disk_mb == 2048
    kwargs = config().as_docker_kwargs()
    assert "storage_opt" not in kwargs
    assert not [key for key in kwargs if "disk" in key or "storage" in key]


def test_the_container_is_not_removed_when_it_exits() -> None:
    """A container that vanishes on exit cannot be inspected after a failure,
    and the Scheduler's reclamation would have nothing to find."""
    assert config().as_docker_kwargs()["auto_remove"] is False


def test_the_environment_carries_nothing_from_this_process() -> None:
    """No inherited environment: this process holds model credentials, and a
    sandbox that could read them would make every other control decorative."""
    assert config().as_docker_kwargs()["environment"] == {"HOME": "/workspace/data"}


def test_the_labels_say_which_run_owns_it() -> None:
    """So a leaked container can be found and attributed after the fact."""
    labels = config().as_docker_kwargs()["labels"]
    assert labels["tiny-hermes.run"] == str(RUN)
    assert labels["tiny-hermes.instance"] == str(INSTANCE)


def test_the_command_keeps_the_container_alive_without_a_shell() -> None:
    """Nothing runs until a tool asks. The container exists to be executed in."""
    assert config().as_docker_kwargs()["command"] == ["sleep", "infinity"]
