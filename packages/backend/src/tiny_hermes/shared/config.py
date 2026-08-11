from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str
    redis_url: str
    s3_endpoint: str
    s3_bucket: str
    session_cookie_secret: str = Field(min_length=32)
    bootstrap_token: str = Field(min_length=32)
    session_ttl_seconds: int = Field(default=28_800, ge=300, le=604_800)

    # Execution tuning. Every bound is explicit so an operator cannot configure
    # a lease shorter than a slice, or a retention window that silently
    # discards events a live subscriber still needs.
    worker_lease_seconds: int = Field(default=30, ge=10, le=300)
    worker_max_slice_seconds: int = Field(default=30, ge=10, le=300)
    worker_idle_poll_seconds: int = Field(default=2, ge=1, le=30)
    worker_shutdown_grace_seconds: int = Field(default=20, ge=5, le=120)
    scheduler_interval_seconds: int = Field(default=5, ge=1, le=60)
    max_recovery_attempts: int = Field(default=3, ge=0, le=10)
    event_retention_hours: int = Field(default=168, ge=1, le=8_760)
    sse_heartbeat_seconds: int = Field(default=15, ge=5, le=60)
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
    sandbox_start_attempts: int = Field(default=3, ge=1, le=5)
    shell_timeout_seconds: int = Field(default=60, ge=1, le=900)
    shell_output_bytes: int = Field(default=1_048_576, ge=1_024, le=10_485_760)

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
