"""Prometheus metrics (observability design §3, §4.1).

Served on a port of the process's own, never the public API port: that one is
what a gateway exposes, and route timings and login failures are not for the
internet. Compose publishes no host port for it either.

No label carries a workspace, user or Run id (design §2). Each such label is a
new series per tenant or per Run, without bound; one Run is looked up in its
trace and logs instead.
"""

import hmac
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from socketserver import ThreadingMixIn
from threading import Thread
from typing import TYPE_CHECKING, Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    make_wsgi_app,  # pyright: ignore[reportUnknownVariableType]
)

if TYPE_CHECKING:
    from tiny_hermes.shared.config import Settings

logger = logging.getLogger(__name__)

#: From a cache hit to a long tool call; a slice is capped at five minutes.
_SECONDS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)

CLAIM_WAIT = Histogram(
    "tiny_hermes_claim_wait_seconds",
    "From a Run becoming claimable to a Worker claiming it.",
    buckets=_SECONDS,
)
WORKER_LANES = Gauge(
    "tiny_hermes_worker_lanes",
    "Worker lanes in this process: state=total while running, state=busy inside a slice.",
    ["state"],
)
RUNS_BY_STATUS = Gauge(
    "tiny_hermes_runs_by_status",
    "Runs not yet finished, by status, read from the database by the Scheduler.",
    ["status"],
)
QUEUE_OLDEST_AGE = Gauge(
    "tiny_hermes_queue_oldest_age_seconds",
    "How long the longest-waiting claimable Run has waited; 0 when none waits.",
)
SCAN_SECONDS = Histogram(
    "tiny_hermes_scheduler_scan_seconds",
    "One Scheduler scan, whatever its outcome.",
    ["scan"],
    buckets=_SECONDS,
)
SCAN_ERRORS = Counter(
    "tiny_hermes_scheduler_scan_errors",
    "Scheduler scans that raised.",
    ["scan"],
)
SCAN_LAST_SUCCESS = Gauge(
    "tiny_hermes_scheduler_scan_last_success_timestamp",
    "When each Scheduler scan last finished without raising.",
    ["scan"],
)

# -- execution (design §3.2) ------------------------------------------------

MODEL_CALL_SECONDS = Histogram(
    "tiny_hermes_model_call_seconds",
    "One model call, by provider kind and how it stopped.",
    ["provider", "outcome"],
    buckets=_SECONDS,
)
MODEL_TOKENS = Counter(
    "tiny_hermes_model_tokens",
    "Tokens the provider reported; a call that reported none adds nothing.",
    ["provider", "direction"],
)
TOOL_CALL_SECONDS = Histogram(
    "tiny_hermes_tool_call_seconds",
    "One tool call, by kind: a tool's name is the Agent author's to choose.",
    ["kind", "outcome"],
    buckets=_SECONDS,
)
SANDBOX_ACQUIRE_SECONDS = Histogram(
    "tiny_hermes_sandbox_acquire_seconds",
    "Getting a sandbox for a round: cold, warm, refused, unreachable or failed.",
    ["outcome"],
    buckets=_SECONDS,
)
WORKSPACE_COMMIT_SECONDS = Histogram(
    "tiny_hermes_workspace_commit_seconds",
    "Committing a round's workspace changes.",
    ["outcome"],
    buckets=_SECONDS,
)
RUNS_FINISHED = Counter(
    "tiny_hermes_runs_finished",
    "Runs that reached a terminal status, counted when the transition commits.",
    ["status"],
)
RECOVERIES = Counter(
    "tiny_hermes_recoveries",
    "Interrupted Runs the Scheduler decided on, counted when the decision commits.",
    ["outcome"],
)

# -- boundaries (design §3.4) -----------------------------------------------

SANDBOXES = Gauge(
    "tiny_hermes_sandboxes",
    "Sandbox instances under a live claim, by instance status.",
    ["state"],
)
SANDBOX_MEMORY_RESERVED = Gauge(
    "tiny_hermes_sandbox_memory_reserved_bytes",
    "The memory limits of every sandbox a live claim may still hold.",
)
SANDBOX_MEMORY_BUDGET = Gauge(
    "tiny_hermes_sandbox_memory_budget_bytes",
    "The sum of sandbox memory limits this controller admits.",
)
SANDBOX_ADMISSION_REJECTED = Counter(
    "tiny_hermes_sandbox_admission_rejected",
    "Acquires refused because the memory budget had no room.",
)
CONTROLLER_ACTION_SECONDS = Histogram(
    "tiny_hermes_controller_action_seconds",
    "One controller action as its caller waits on it, Docker and rows together.",
    ["action", "outcome"],
    buckets=_SECONDS,
)
EGRESS_CONNECTIONS = Counter(
    "tiny_hermes_egress_connections",
    "Connections the egress proxy decided on. Never by host: the Agent picks hosts.",
    ["caller", "decision", "reason"],
)

# -- entry (design §3.5) ----------------------------------------------------

HTTP_REQUEST_SECONDS = Histogram(
    "tiny_hermes_http_request_seconds",
    "API requests by route template, never by path: a path carries ids.",
    ["route", "method", "status_class"],
    buckets=_SECONDS,
)
SSE_CONNECTIONS = Gauge(
    "tiny_hermes_sse_connections",
    "Run event streams open on this API process.",
)
LOGIN_ATTEMPTS = Counter(
    "tiny_hermes_login_attempts",
    "Password logins by outcome.",
    ["outcome"],
)


async def timed[T](histogram: Histogram, work: Awaitable[T]) -> T:
    """Await `work`, observing its time under outcome ok or failed."""
    started = time.monotonic()
    try:
        value = await work
    except BaseException:
        histogram.labels(outcome="failed").observe(time.monotonic() - started)
        raise
    histogram.labels(outcome="ok").observe(time.monotonic() - started)
    return value


class _Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _Quiet(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base's name
        """A scrape every fifteen seconds is not news."""


def serve_metrics(
    *, host: str, port: int, token: str | None = None
) -> tuple[WSGIServer, Thread]:
    """Serve the registry from a daemon thread; port 0 picks a free one.

    With a token, only `Authorization: Bearer <token>` is answered. The egress
    proxy needs it: a sandbox can reach every port the proxy listens on, and
    what it would read there is every workspace's outbound decisions (design
    §4.2). Any other process may set one too.
    """
    registry: _App = make_wsgi_app()  # pyright: ignore[reportUnknownVariableType]
    app = _bearer_only(registry, token) if token else registry
    server = make_server(host, port, app, server_class=_Server, handler_class=_Quiet)
    thread = Thread(target=server.serve_forever, name="metrics", daemon=True)
    thread.start()
    return server, thread


def serve_configured_metrics(settings: "Settings", *, needs_token: bool = False) -> None:
    """What every process's entry point calls once, before its loop starts."""
    if not settings.metrics_port:
        return
    if needs_token and not settings.metrics_token:
        logger.warning("metrics not served: METRICS_TOKEN is unset and this process requires it")
        return
    serve_metrics(
        host=settings.metrics_host,
        port=settings.metrics_port,
        token=settings.metrics_token or None,
    )
    logger.info("metrics served", extra={"port": settings.metrics_port})


_App = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]


def _bearer_only(app: _App, token: str) -> _App:
    expected = f"Bearer {token}".encode()

    def guarded(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        presented = str(environ.get("HTTP_AUTHORIZATION", "")).encode()
        if not hmac.compare_digest(presented, expected):
            start_response("401 Unauthorized", [("Content-Type", "text/plain")])
            return [b"metrics need a bearer token\n"]
        return app(environ, start_response)

    return guarded
