from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tiny_hermes.egress.domain.decision import ALLOWED_PORTS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str
    redis_url: str
    s3_endpoint: str
    s3_bucket: str
    #: Required since phase 3C: the platform writes real objects, so a
    #: deployment without credentials must fail at startup rather than at the
    #: first checkpoint. The Controller process never receives these — it has
    #: Docker and must not gain MinIO.
    # Required to be *present*, allowed to be empty: the controller runs with
    # both deliberately blank — it must never hold an object-store credential
    # (design §14.6) — while a deployment that forgot to set them at all still
    # fails at startup instead of at its first checkpoint.
    s3_access_key: str
    s3_secret_key: str
    session_cookie_secret: str = Field(min_length=32)
    bootstrap_token: str = Field(min_length=32)
    session_ttl_seconds: int = Field(default=28_800, ge=300, le=604_800)
    #: End-user entry design §4.2: independent of `session_ttl_seconds` on
    #: purpose — the two identity systems must never share a setting any more
    #: than they share a cookie name, or a change meant for one silently
    #: reaches the other. Same 8-hour default as the design's own number, not
    #: because the two happen to agree today.
    end_user_session_ttl_seconds: int = Field(default=28_800, ge=300, le=604_800)

    # Execution tuning. Every bound is explicit so an operator cannot configure
    # a lease shorter than a slice, or a retention window that silently
    # discards events a live subscriber still needs.
    # §24.1: after the Worker is killed, a retry-safe Run is queued in 30s.
    # A 30s lease plus a 5s scan cannot meet that cell: expiry is already
    # the whole window. 20s + 1s scan fits; the lease is not a §24.1 number.
    worker_lease_seconds: int = Field(default=20, ge=10, le=300)
    worker_max_slice_seconds: int = Field(default=30, ge=10, le=300)
    worker_idle_poll_seconds: int = Field(default=2, ge=1, le=30)
    worker_shutdown_grace_seconds: int = Field(default=20, ge=5, le=120)
    scheduler_interval_seconds: int = Field(default=1, ge=1, le=60)
    max_recovery_attempts: int = Field(default=3, ge=0, le=10)
    event_retention_hours: int = Field(default=168, ge=1, le=8_760)
    sse_heartbeat_seconds: int = Field(default=15, ge=5, le=60)
    #: The API process holds one pool. §24.1 keeps 500 SSE subscribers, each
    #: polling committed rows; the default SQLAlchemy pool of 15 serialises
    #: those polls into multi-second gaps. Worker and Scheduler do not read
    #: this — they keep the small engine default.
    database_pool_size: int = Field(default=80, ge=5, le=200)
    database_max_overflow: int = Field(default=40, ge=0, le=200)
    deterministic_model_delay_ms: int = Field(default=50, ge=0, le=5_000)

    # Outbound calls. The read timeout is what actually bounds one model round:
    # the slice budget is checked between rounds and never inside one.
    outbound_connect_timeout_seconds: float = Field(default=5.0, ge=0.5, le=60.0)
    outbound_read_timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    outbound_max_redirects: int = Field(default=5, ge=0, le=10)
    outbound_max_response_bytes: int = Field(default=10_485_760, ge=1_024, le=104_857_600)
    #: Comma-separated CIDRs a platform administrator has approved, which is how
    #: an enterprise private model endpoint becomes reachable at all. Only
    #: private and carrier-grade NAT ranges can be opened this way; loopback and
    #: link-local stay refused however wide the range is.
    outbound_allowed_cidrs: str = ""

    #: The egress proxy (product design §16.5). Empty by default in both
    #: fields, which means a deployment that has not stood one up sends
    #: nothing: `EGRESS_PROXY_URL` unset makes every outbound call refuse
    #: rather than connect directly, and `EGRESS_ALLOWED_HOSTS` unset makes the
    #: platform layer approve nothing. Both are the same shape as an empty
    #: `SANDBOX_IMAGE_DIGEST` — unconfigured fails closed and says so.
    egress_proxy_url: str = ""
    #: What a platform process presents to the proxy. It answers "is this one of
    #: ours"; the scope still comes from the directory, so a leaked token widens
    #: nothing by itself.
    egress_proxy_token: str = ""
    egress_proxy_port: int = Field(default=3128, ge=1, le=65_535)
    #: Ports a target may listen on, comma separated. Empty means the shipped
    #: pair, 80 and 443.
    #:
    #: Widening this is a platform administrator's decision and only theirs,
    #: which is the shape §16.5 gives every widening. It is needed more often
    #: than the default suggests: an enterprise's own API on 8443 is ordinary,
    #: and without this an installation could bind only tools that happen to
    #: live on the public web's two ports.
    egress_allowed_ports: str = ""
    #: The platform outbound scope, comma separated: hosts, one-level wildcards
    #: and networks. §4 of the M2C-1 plan moves this into the database; a
    #: single-tenant installation is served by the setting alone.
    egress_allowed_hosts: str = ""
    #: Where this deployment's console can be reached, for links inside
    #: messages the platform sends out. Empty on purpose by default: the
    #: address of a private installation's console is known only to whoever
    #: deployed it, and a guessed URL is a dead link in front of a user.
    #: §19.2 allows `必要的审批卡片或跳转管理页面`, and this is the second.
    console_base_url: str = ""
    #: The Docker network a sandbox is attached to. Empty means a sandbox has
    #: no network at all — §16.4's default — rather than one nobody guards.
    #: The network itself must reach the proxy and nothing else; Compose
    #: declares it `internal` so that is a property of the deployment rather
    #: than of anybody's discipline.
    sandbox_egress_network: str = ""

    #: The most model rounds an Agent author may ask for (product design §12.3,
    #: M2A design §4.7). 20 was a literal on `AgentLimits.max_model_calls`
    #: chosen when a Run could not run long; a Goal loop that must finish inside
    #: 20 calls cannot do much. Checked when a draft is saved and again when it
    #: is published — never when a published version is read back, so lowering
    #: this cannot break an Agent already published above it.
    agent_max_model_calls: int = Field(default=20, ge=1, le=200)

    #: Attempts per model round, on the same endpoint. A workspace may lower
    #: this; nothing may raise it.
    model_max_attempts: int = Field(default=3, ge=1, le=3)
    model_retry_base_ms: int = Field(default=250, ge=10, le=5_000)

    #: The Sandbox Controller's socket, in the volume the Worker and Scheduler
    #: share with it. They mount this and nothing else; the Docker socket is the
    #: Controller's alone.
    sandbox_controller_socket: str = "/run/tiny-hermes/controller.sock"
    #: The group the platform's own processes run as, from the application
    #: image. The Controller runs as root and hands the socket to this group so
    #: the Worker and the Scheduler can open it without it being world-writable.
    sandbox_socket_gid: int = 10001
    #: The approved runtime image, by digest. Empty by default, and an empty
    #: allowlist approves nothing — a deployment that has not chosen an image
    #: cannot run a tool, which is the correct way for this to fail.
    sandbox_image_digest: str = ""
    #: How long a frozen instance stays warm for its own Run. §11.4 allows
    #: 0–30 minutes and lets a workspace lower it, never raise it.
    sandbox_idle_ttl_seconds: int = Field(default=300, ge=0, le=1_800)
    #: The cache tmpfs ceilings (design §9). Kernel-enforced; the profile the
    #: controller hands Docker is built from these at startup.
    sandbox_cache_mb: int = Field(default=512, ge=64, le=4_096)
    sandbox_cache_inodes: int = Field(default=200_000, ge=10_000, le=1_000_000)
    sandbox_start_attempts: int = Field(default=3, ge=1, le=5)
    shell_timeout_seconds: int = Field(default=60, ge=1, le=900)
    shell_output_bytes: int = Field(default=1_048_576, ge=1_024, le=10_485_760)

    # The SessionWorkspace, design §15. `workspace_max_bytes` and
    # `workspace_max_objects` are a **checkpoint quota** — content over them
    # cannot become a committed revision. They are not a physical host-disk
    # limit while an arbitrary command is still running, and no document may
    # describe them as one.
    workspace_max_bytes: int = Field(default=2_147_483_648, ge=1_048_576, le=1_099_511_627_776)
    workspace_max_objects: int = Field(default=100_000, ge=100, le=10_000_000)
    workspace_file_list_entries: int = Field(default=1_000, ge=10, le=10_000)
    workspace_file_read_bytes: int = Field(default=1_048_576, ge=1_024, le=104_857_600)
    workspace_file_write_bytes: int = Field(default=16_777_216, ge=1_024, le=1_073_741_824)
    workspace_staging_ttl_seconds: int = Field(default=86_400, ge=3_600, le=604_800)
    #: Total deadline for one workspace import or export. Execute has its own
    #: command timeout and is not bounded by this, so a legal silent 15-minute
    #: command is not killed by a transfer rule.
    workspace_transfer_timeout_seconds: int = Field(default=300, ge=30, le=1_800)
    controller_stream_frame_bytes: int = Field(default=1_048_576, ge=4_096, le=1_048_576)
    controller_stream_credit_bytes: int = Field(default=8_388_608, ge=1_048_576, le=67_108_864)
    controller_stream_idle_seconds: int = Field(default=30, ge=5, le=300)
    artifact_max_bytes: int = Field(default=104_857_600, ge=1_048_576, le=1_073_741_824)
    run_artifact_max_bytes: int = Field(default=524_288_000, ge=1_048_576, le=10_737_418_240)

    #: Envelope KEK. Empty is legal at process boot (Workers unwrap at call
    #: time). The API ready check refuses an empty or invalid value.
    tiny_hermes_kek: str = ""
    tiny_hermes_kek_id: str = "v1"
    tiny_hermes_previous_kek: str = ""
    tiny_hermes_previous_kek_id: str = ""

    @property
    def approved_image_digests(self) -> tuple[str, ...]:
        return (self.sandbox_image_digest,) if self.sandbox_image_digest else ()

    @field_validator("outbound_allowed_cidrs")
    @classmethod
    def reject_unparseable_cidrs(cls, value: str) -> str:
        for entry in (part.strip() for part in value.split(",")):
            if entry:
                ip_network(entry)
        return value

    @field_validator("egress_allowed_ports")
    @classmethod
    def reject_unparseable_ports(cls, value: str) -> str:
        for entry in (part.strip() for part in value.split(",")):
            if not entry:
                continue
            port = int(entry)
            if not 1 <= port <= 65_535:
                raise ValueError(f"{port} is not a port")
        return value

    @property
    def allowed_ports(self) -> frozenset[int]:
        """What the proxy will connect to, or the shipped pair when unset.

        Empty falls back rather than approving nothing: a port list is a
        widening of a default that already works, unlike a scope, where empty
        must mean "nothing" because the whole point is that it approves.
        """
        chosen = frozenset(
            int(part.strip()) for part in self.egress_allowed_ports.split(",") if part.strip()
        )
        return chosen or ALLOWED_PORTS

    @property
    def approved_networks(self) -> tuple[IPv4Network | IPv6Network, ...]:
        return tuple(
            ip_network(part.strip())
            for part in self.outbound_allowed_cidrs.split(",")
            if part.strip()
        )

    @field_validator("session_cookie_secret", "bootstrap_token")
    @classmethod
    def reject_example_secrets(cls, value: str) -> str:
        if value in {"change-me", "example", "secret"}:
            raise ValueError("example secrets are not valid runtime secrets")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
