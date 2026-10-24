from typing import Any

import pytest
from pydantic import ValidationError
from tiny_hermes.shared.config import Settings


def _settings(**overrides: Any) -> Settings:
    """Settings built from these values alone.

    `_env_file=None` is the point: `Settings` reads `.env`, and *First start*
    in docs/development.md creates one with a real KEK in it. Without this,
    a developer who followed the documentation in order — first start, then
    local tests — met a red suite, while CI, which has no `.env`, stayed
    green. A unit test must not depend on whether the machine it runs on has
    been set up.
    """
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://app:app@db/app",
        "redis_url": "redis://redis:6379/0",
        "s3_endpoint": "http://minio:9000",
        "s3_bucket": "tiny-hermes",
        "s3_access_key": "test-access-key",
        "s3_secret_key": "test-secret-key",
        "session_cookie_secret": "cookie-secret-with-at-least-32-characters",
        "bootstrap_token": "bootstrap-token-with-at-least-32-characters",
    }
    values.update(overrides)
    # `_env_file` is pydantic-settings' own keyword; its signature does not
    # declare it, so the type checker has to be told this is deliberate.
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


EXECUTION_BOUNDS = [
    ("worker_lease_seconds", 20, 10, 300),
    ("worker_max_slice_seconds", 30, 10, 300),
    ("worker_idle_poll_seconds", 2, 1, 30),
    ("worker_shutdown_grace_seconds", 20, 5, 120),
    ("scheduler_interval_seconds", 1, 1, 60),
    ("max_recovery_attempts", 3, 0, 10),
    ("event_retention_hours", 168, 1, 8760),
    ("sse_heartbeat_seconds", 15, 5, 60),
    ("database_pool_size", 80, 5, 200),
    ("database_max_overflow", 40, 0, 200),
    ("deterministic_model_delay_ms", 50, 0, 5000),
    ("worker_concurrency", 1, 1, 64),
]


def test_settings_reject_placeholder_cookie_secret() -> None:
    with pytest.raises(ValidationError):
        _settings(session_cookie_secret="change-me")


@pytest.mark.parametrize(("name", "default", "low", "high"), EXECUTION_BOUNDS)
def test_execution_settings_default_inside_their_documented_range(
    name: str, default: int, low: int, high: int
) -> None:
    assert getattr(_settings(), name) == default
    assert low <= default <= high


@pytest.mark.parametrize(("name", "default", "low", "high"), EXECUTION_BOUNDS)
def test_execution_settings_accept_their_boundaries(
    name: str, default: int, low: int, high: int
) -> None:
    del default
    assert getattr(_settings(**{name: low}), name) == low
    assert getattr(_settings(**{name: high}), name) == high


@pytest.mark.parametrize(("name", "default", "low", "high"), EXECUTION_BOUNDS)
def test_execution_settings_reject_values_outside_their_range(
    name: str, default: int, low: int, high: int
) -> None:
    del default
    with pytest.raises(ValidationError):
        _settings(**{name: low - 1})
    with pytest.raises(ValidationError):
        _settings(**{name: high + 1})


# Design §15's 3C defaults. `workspace_max_bytes` is a checkpoint quota — what
# may become a committed revision — never a physical host-disk limit.
WORKSPACE_BOUNDS = [
    ("workspace_max_bytes", 2_147_483_648, 1_048_576, 1_099_511_627_776),
    ("workspace_max_objects", 100_000, 100, 10_000_000),
    ("workspace_file_list_entries", 1_000, 10, 10_000),
    ("workspace_file_read_bytes", 1_048_576, 1_024, 104_857_600),
    ("workspace_file_write_bytes", 16_777_216, 1_024, 1_073_741_824),
    ("workspace_staging_ttl_seconds", 86_400, 3_600, 604_800),
    ("workspace_transfer_timeout_seconds", 300, 30, 1_800),
    ("controller_stream_frame_bytes", 1_048_576, 4_096, 1_048_576),
    ("controller_stream_credit_bytes", 8_388_608, 1_048_576, 67_108_864),
    ("controller_stream_idle_seconds", 30, 5, 300),
    ("artifact_max_bytes", 104_857_600, 1_048_576, 1_073_741_824),
    ("run_artifact_max_bytes", 524_288_000, 1_048_576, 10_737_418_240),
    ("sandbox_cache_mb", 512, 64, 4_096),
    ("sandbox_memory_mb", 1_024, 256, 32_768),
    ("sandbox_cache_inodes", 200_000, 10_000, 1_000_000),
]


@pytest.mark.parametrize(("name", "default", "low", "high"), WORKSPACE_BOUNDS)
def test_workspace_settings_default_inside_their_documented_range(
    name: str, default: int, low: int, high: int
) -> None:
    assert getattr(_settings(), name) == default
    assert low <= default <= high


@pytest.mark.parametrize(("name", "default", "low", "high"), WORKSPACE_BOUNDS)
def test_workspace_settings_reject_values_outside_their_range(
    name: str, default: int, low: int, high: int
) -> None:
    del default
    with pytest.raises(ValidationError):
        _settings(**{name: low - 1})
    with pytest.raises(ValidationError):
        _settings(**{name: high + 1})


def test_settings_default_kek_is_empty_so_a_worker_can_boot() -> None:
    assert _settings().tiny_hermes_kek == ""
    assert _settings().tiny_hermes_kek_id == "v1"


def test_a_transfer_timeout_above_thirty_minutes_is_refused() -> None:
    """Named in design §15: 300 by default, configurable up to 1,800."""
    assert _settings(workspace_transfer_timeout_seconds=1_800) is not None
    with pytest.raises(ValidationError):
        _settings(workspace_transfer_timeout_seconds=1_801)


@pytest.mark.parametrize("missing", ["s3_access_key", "s3_secret_key"])
def test_object_store_credentials_are_required(
    missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C writes real objects, so a deployment without credentials must not start."""
    monkeypatch.delenv(missing.upper(), raising=False)
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://app:app@db/app",
        "redis_url": "redis://redis:6379/0",
        "s3_endpoint": "http://minio:9000",
        "s3_bucket": "tiny-hermes",
        "s3_access_key": "test-access-key",
        "s3_secret_key": "test-secret-key",
        "session_cookie_secret": "cookie-secret-with-at-least-32-characters",
        "bootstrap_token": "bootstrap-token-with-at-least-32-characters",
    }
    del values[missing]
    # `_env_file=None` keeps a developer's local .env from satisfying the
    # requirement this test exists to prove.
    values["_env_file"] = None
    with pytest.raises(ValidationError):
        Settings(**values)


def test_a_deployment_that_says_nothing_gets_the_web_s_two_ports() -> None:
    """Empty falls back rather than approving nothing.

    Unlike a scope — where empty must mean "nothing", because approving is the
    whole point — a port list is a widening of a default that already works.
    """
    assert _settings().allowed_ports == frozenset({80, 443})


def test_a_platform_administrator_may_widen_the_ports() -> None:
    """§16.5's shape for every widening: explicit, and only at the platform
    level. Needed more often than the default suggests — an enterprise's own
    API on 8443 is ordinary, and without this an installation could bind only
    tools that happen to live on the public web's two ports."""
    widened = _settings(egress_allowed_ports="80, 443, 8443")

    assert widened.allowed_ports == frozenset({80, 443, 8443})


@pytest.mark.parametrize("value", ["0", "65536", "http", "-1"])
def test_a_port_that_is_not_one_stops_the_process_starting(value: str) -> None:
    """Refused where an operator is looking, rather than at the first call."""
    with pytest.raises(ValidationError):
        _settings(egress_allowed_ports=value)


def test_the_sandbox_memory_budget_defaults_to_half_the_host_at_controller_start() -> None:
    # None here; the controller reads the host's memory, which Settings cannot.
    assert _settings().sandbox_memory_budget_mb is None
    assert _settings(sandbox_memory_budget_mb=4_096).sandbox_memory_budget_mb == 4_096


def test_a_budget_smaller_than_one_sandbox_is_refused() -> None:
    # It would admit nothing, and every Run with a tool would wait forever.
    assert _settings(sandbox_memory_mb=512, sandbox_memory_budget_mb=512) is not None
    with pytest.raises(ValidationError, match="sandbox_memory_budget_mb"):
        _settings(sandbox_memory_mb=1_024, sandbox_memory_budget_mb=1_023)


def test_an_empty_budget_in_the_environment_means_half_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose passes `${SANDBOX_MEMORY_BUDGET_MB:-}`, which is "" when unset;
    parsed as an integer that stopped every service sharing the variable."""
    monkeypatch.setenv("SANDBOX_MEMORY_BUDGET_MB", "")

    assert _settings().sandbox_memory_budget_mb is None


DEFAULT_CONTROLLER = "unix:///run/tiny-hermes/controller.sock"


def test_a_deployment_that_says_nothing_finds_the_controller_where_it_always_was() -> None:
    settings = _settings()

    assert str(settings.controller_address) == DEFAULT_CONTROLLER
    assert str(settings.controller_listen_address) == DEFAULT_CONTROLLER


def test_the_caller_and_the_controller_are_configured_separately() -> None:
    # Separately because in tier B they are on different hosts: the Worker
    # names the Controller's host, the Controller names its own interface.
    settings = _settings(
        sandbox_controller_url="unix:///run/a/controller.sock",
        sandbox_controller_listen="unix:///run/b/controller.sock",
    )

    assert settings.controller_address.path == "/run/a/controller.sock"
    assert settings.controller_listen_address.path == "/run/b/controller.sock"


def test_the_old_socket_setting_still_names_both_ends() -> None:
    settings = _settings(sandbox_controller_socket="/run/old/controller.sock")

    assert settings.controller_address.path == "/run/old/controller.sock"
    assert settings.controller_listen_address.path == "/run/old/controller.sock"


@pytest.mark.parametrize("new", ["sandbox_controller_url", "sandbox_controller_listen"])
def test_the_old_and_new_spellings_together_are_refused(new: str) -> None:
    # Which one wins would be a guess, and a wrong guess is a Worker talking
    # to a socket nobody listens on.
    with pytest.raises(ValidationError, match="SANDBOX_CONTROLLER_SOCKET"):
        _settings(
            sandbox_controller_socket="/run/old/controller.sock",
            **{new: "unix:///run/new/controller.sock"},
        )


@pytest.mark.parametrize("field", ["sandbox_controller_url", "sandbox_controller_listen"])
def test_an_address_that_does_not_parse_stops_the_process_starting(field: str) -> None:
    with pytest.raises(ValidationError, match="tls://"):
        _settings(**{field: "tcp://sandbox-1.internal:7443"})


@pytest.mark.parametrize(
    "variable",
    ["SANDBOX_CONTROLLER_URL", "SANDBOX_CONTROLLER_LISTEN", "SANDBOX_CONTROLLER_SOCKET"],
)
def test_an_empty_controller_variable_means_unset(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`${VAR:-}` in Compose is "" when unset, which must not read as a path."""
    monkeypatch.setenv(variable, "")

    assert str(_settings().controller_address) == DEFAULT_CONTROLLER


TLS_FILES = {
    "sandbox_controller_ca_file": "/etc/tiny-hermes/controller/ca.pem",
    "sandbox_controller_cert_file": "/etc/tiny-hermes/controller/worker.pem",
    "sandbox_controller_key_file": "/etc/tiny-hermes/controller/worker.key",
}


@pytest.mark.parametrize("missing", sorted(TLS_FILES))
@pytest.mark.parametrize(
    "end", ["sandbox_controller_url", "sandbox_controller_listen"]
)
def test_tls_at_either_end_needs_all_three_files(missing: str, end: str) -> None:
    files = {name: path for name, path in TLS_FILES.items() if name != missing}
    with pytest.raises(ValidationError, match=missing.upper()):
        _settings(**{end: "tls://10.0.0.5:7443"}, **files)


def test_tls_with_its_files_is_accepted_and_handed_on() -> None:
    settings = _settings(sandbox_controller_url="tls://sandbox-1.internal:7443", **TLS_FILES)

    files = settings.controller_tls_files
    assert files is not None
    assert files.cert_file == "/etc/tiny-hermes/controller/worker.pem"


def test_a_unix_deployment_has_no_tls_files_to_hand_on() -> None:
    assert _settings().controller_tls_files is None


@pytest.mark.parametrize("anywhere", ["tls://0.0.0.0:7443", "tls://[::]:7443"])
def test_the_controller_does_not_listen_everywhere_unless_told_to(anywhere: str) -> None:
    # It holds the Docker socket; every interface includes ones nobody meant.
    with pytest.raises(ValidationError, match="SANDBOX_CONTROLLER_LISTEN_ANY"):
        _settings(sandbox_controller_listen=anywhere, **TLS_FILES)

    allowed = _settings(
        sandbox_controller_listen=anywhere, sandbox_controller_listen_any=True, **TLS_FILES
    )
    assert allowed.controller_listen_address.port == 7443


@pytest.mark.parametrize("field", sorted(TLS_FILES))
def test_an_empty_tls_file_variable_means_unset(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(field.upper(), "")

    assert getattr(_settings(), field) is None


#: What `deploy/compose/compose.yaml` falls back to when a variable is unset.
#: Fine on a laptop; outside development each one is a published secret.
COMPOSE_DEFAULTS = [
    ("session_cookie_secret", "local-cookie-secret-with-32-characters"),
    ("bootstrap_token", "local-bootstrap-token-with-32-characters"),
    ("tiny_hermes_kek", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="),
    ("s3_access_key", "tiny-hermes-local"),
    ("s3_secret_key", "tiny-hermes-local-password"),
    ("database_url", "postgresql+asyncpg://tiny_hermes:local-only@postgres:5432/tiny_hermes"),
]


@pytest.mark.parametrize(("name", "value"), COMPOSE_DEFAULTS)
def test_outside_development_the_compose_defaults_are_refused(name: str, value: str) -> None:
    with pytest.raises(ValidationError, match=name.upper()):
        _settings(environment="production", **{name: value})


@pytest.mark.parametrize(("name", "value"), COMPOSE_DEFAULTS)
def test_in_development_the_compose_defaults_still_start(name: str, value: str) -> None:
    """The local stack and CI run on them; refusing here would only move the
    default into every developer's `.env`."""
    assert getattr(_settings(**{name: value}), name) == value


def test_the_sandbox_memory_ceiling_may_not_sit_below_the_instance_default() -> None:
    """§21.3, v2.22: a default above the ceiling would be a size no Agent
    could ask for and every Agent that asks for nothing gets."""
    with pytest.raises(ValidationError, match="sandbox_ceiling_memory_mb"):
        _settings(sandbox_memory_mb=2048, sandbox_ceiling_memory_mb=1024)


def test_the_sandbox_memory_ceiling_may_equal_the_default() -> None:
    assert _settings(sandbox_memory_mb=2048, sandbox_ceiling_memory_mb=2048)


def test_a_lone_sandbox_host_is_called_default() -> None:
    """Tier C: every row written before there could be two hosts is `default`,
    so a single host must keep that name to still own them."""
    assert _settings().sandbox_controller_name == "default"


@pytest.mark.parametrize("name", ["sbx-a", "host7", "a"])
def test_a_sandbox_host_name_is_a_short_lowercase_word(name: str) -> None:
    assert _settings(sandbox_controller_name=name).sandbox_controller_name == name


@pytest.mark.parametrize("name", ["", "SBX", "sbx a", "sbx/a", "-sbx", "x" * 64])
def test_a_sandbox_host_name_that_could_be_mistaken_is_refused(name: str) -> None:
    """It is a key in the database and a word in logs and audit; a path, a
    space or a case difference would be two hosts that look like one."""
    with pytest.raises(ValidationError, match="sandbox_controller_name"):
        _settings(sandbox_controller_name=name)


@pytest.mark.parametrize("url", ["tls://10.0.0.5:7700", "tls://sbx-a.internal:7700"])
def test_a_sandbox_host_advertises_a_tls_address(url: str) -> None:
    assert _settings(sandbox_controller_advertise_url=url).sandbox_controller_advertise_url == url


@pytest.mark.parametrize("url", ["unix:///run/c.sock", "http://10.0.0.5:7700", "10.0.0.5:7700"])
def test_a_sandbox_host_never_advertises_anything_but_tls(url: str) -> None:
    """Tier C §4.1: another host dials what is advertised, and only mutual TLS
    crosses a network; a socket path is a path on this host alone."""
    with pytest.raises(ValidationError, match="sandbox_controller_advertise_url"):
        _settings(sandbox_controller_advertise_url=url)


def test_an_empty_advertised_address_is_none() -> None:
    """Compose passes `${SANDBOX_CONTROLLER_ADVERTISE_URL:-}`, "" when unset:
    a single host on a socket, which advertises nothing."""
    assert _settings(sandbox_controller_advertise_url="").sandbox_controller_advertise_url is None


_TLS_FILES = {
    "sandbox_controller_ca_file": "/etc/tiny-hermes/ca.pem",
    "sandbox_controller_cert_file": "/etc/tiny-hermes/worker.pem",
    "sandbox_controller_key_file": "/etc/tiny-hermes/worker.key",
}


def test_a_worker_may_be_pointed_at_a_pool_of_sandbox_hosts() -> None:
    """Tier C §4.9: `pool` reads the hosts from the database instead of
    naming one."""
    settings = _settings(sandbox_controller_url="pool", **_TLS_FILES)

    assert settings.sandbox_pool is True


def test_a_pool_is_reached_only_over_tls() -> None:
    """Every host in a pool is another machine, and only mutual TLS crosses a
    network."""
    with pytest.raises(ValidationError, match="pool"):
        _settings(sandbox_controller_url="pool")


def test_one_named_controller_is_not_a_pool() -> None:
    assert _settings().sandbox_pool is False


def test_placement_skips_a_host_silent_for_thirty_seconds_unless_told_otherwise() -> None:
    assert _settings().sandbox_host_stale_seconds == 30


def test_sandboxes_run_on_runc_unless_told_otherwise() -> None:
    assert _settings().sandbox_runtime == "runc"
    assert _settings(sandbox_runtime="runsc").sandbox_runtime == "runsc"


@pytest.mark.parametrize("runtime", ["", "run sc", "../runsc", "RUNSC;rm"])
def test_a_runtime_name_that_is_not_a_name_is_refused(runtime: str) -> None:
    with pytest.raises(ValidationError, match="sandbox_runtime"):
        _settings(sandbox_runtime=runtime)
