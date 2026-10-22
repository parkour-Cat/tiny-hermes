"""Prometheus metrics (observability design §3, §4.1).

Served on a port of the process's own, never the public API port: that one is
what a gateway exposes, and route timings and login failures are not for the
internet. Compose publishes no host port for it either.

No label carries a workspace, user or Run id (design §2). Each such label is a
new series per tenant or per Run, without bound; one Run is looked up in its
trace and logs instead.
"""

from threading import Thread
from wsgiref.simple_server import WSGIServer

from prometheus_client import Counter, Gauge, Histogram, start_http_server

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


def serve_metrics(*, host: str, port: int) -> tuple[WSGIServer, Thread]:
    """Serve the registry from a daemon thread; port 0 picks a free one."""
    return start_http_server(port, addr=host)
