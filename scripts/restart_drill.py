"""Restart the platform's processes under load and prove no committed work is lost.

Four failures an operator will actually meet, run against a live Compose stack:
the Worker is restarted while it is executing, Redis disappears and comes back,
the Worker dies without warning and is recovered by the Scheduler, and a Worker
dies while a sandbox command is still running.

The drill never removes state. It stops, starts, kills and restarts containers;
it does not take the stack down and it does not touch a volume. A drill that
begins by deleting the state it claims to preserve proves nothing, so
``FORBIDDEN`` refuses those arguments rather than trusting this file to stay
careful.

Nothing here prints a cookie, a token, a password, a database URL, or the text
of an Agent's personality or a Run's input. What it prints is identifiers,
statuses, event sequences, and timings.

Usage::

    DETERMINISTIC_MODEL_DELAY_MS=3000 \\
      docker compose -f deploy/compose/compose.yaml up -d --build --wait
    DETERMINISTIC_MODEL_DELAY_MS=3000 uv run --no-sync python scripts/restart_drill.py
"""

import json
import os
import subprocess  # noqa: S404 - restarting containers is this script's whole purpose
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import httpx

COMPOSE_FILE = "deploy/compose/compose.yaml"
API = os.environ.get("TINY_HERMES_API", "http://127.0.0.1:8000")
PROJECT = os.environ.get("COMPOSE_PROJECT_NAME")

ADMIN_SUBJECT = os.environ.get("TINY_HERMES_DRILL_SUBJECT", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("TINY_HERMES_DRILL_PASSWORD", "long-pass-123")
BOOTSTRAP_TOKEN = os.environ.get(
    "TINY_HERMES_E2E_BOOTSTRAP_TOKEN", "local-bootstrap-token-with-32-characters"
)

CSRF_COOKIE = "tiny_hermes_csrf"

#: Arguments the drill must never send to Compose. Losing a volume is the one
#: mistake a state-preservation drill cannot recover from or apologise for.
FORBIDDEN = frozenset({"down", "rm", "-v", "--volumes", "prune"})

#: Below this a Run finishes before a restart can land inside it, and every
#: scenario here becomes a test of nothing.
MINIMUM_MODEL_DELAY_MS = 1_000

#: ``worker_idle_poll_seconds``. A Worker that is never woken waits at most this
#: long before going to look for work itself.
IDLE_POLL_SECONDS = 2.0

#: How many Runs the drill submits when asking whether wake-ups came back.
WAKE_UP_ATTEMPTS = 3

#: A held lease expiring (30s) plus a Scheduler interval (5s), plus room to be slow.
RECOVERY_TIMEOUT = 120.0
RUN_TIMEOUT = 180.0
#: Statuses a Run will not leave. Waiting for `completed` while the Run is
#: already `failed` must fail immediately — compose-e2e burned 300s polling.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def compose(*arguments: str) -> None:
    forbidden = FORBIDDEN.intersection(arguments)
    if forbidden:
        raise SystemExit(f"the drill never removes state: {sorted(forbidden)}")
    project = ["-p", PROJECT] if PROJECT else []
    command = ["docker", "compose", *project, "-f", COMPOSE_FILE, *arguments]
    print(f"  $ docker compose {' '.join(arguments)}")
    subprocess.run(  # noqa: S603 - every argument is a literal from this file
        command, check=True
    )


def await_healthy(service: str, timeout: float = 90.0) -> None:
    """Waits for Compose to call the service healthy, so a restart is really over."""
    project = ["-p", PROJECT] if PROJECT else []
    command = [
        "docker", "compose", *project, "-f", COMPOSE_FILE,
        "ps", "--format", "{{.Service}} {{.Health}}",
    ]  # fmt: skip
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listed = subprocess.run(  # noqa: S603 - every argument is a literal from this file
            command, check=True, capture_output=True, text=True
        )
        for row in listed.stdout.splitlines():
            name, _, health = row.partition(" ")
            if name == service and health.strip() == "healthy":
                return
        time.sleep(1)
    raise SystemExit(f"{service} did not become healthy within {timeout:.0f}s")


@dataclass(frozen=True)
class Event:
    sequence: int
    event_type: str


class Console:
    """The platform as a signed-in operator meets it, over plain HTTP."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def _headers(self, workspace: str | None = None, **extra: str) -> dict[str, str]:
        # Unquoted, because the header is compared against the token itself: a
        # value the server had to percent-encode on its way into the cookie
        # would otherwise be echoed back still encoded and refused.
        token = unquote(self._client.cookies.get(CSRF_COOKIE) or "")
        headers = {"X-CSRF-Token": token, **extra}
        if workspace is not None:
            headers["X-Workspace-Id"] = workspace
        return headers

    def sign_in(self) -> None:
        opened = self._client.post(
            "/api/v1/bootstrap",
            headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
            json={
                "subject": ADMIN_SUBJECT,
                "display_name": "Admin",
                "password": ADMIN_PASSWORD,
            },
        )
        # A stack the acceptance suite already opened answers `bootstrap_closed`,
        # which is not a failure here. Any other refusal is.
        if opened.status_code not in (201, 409):
            opened.raise_for_status()
        self._client.post(
            "/api/v1/auth/sessions",
            json={"subject": ADMIN_SUBJECT, "password": ADMIN_PASSWORD},
        ).raise_for_status()

    def create_workspace(self, name: str) -> str:
        created = self._client.post(
            "/api/v1/workspaces", headers=self._headers(), json={"name": name}
        )
        created.raise_for_status()
        return str(created.json()["id"])

    def publish_agent(
        self, workspace: str, alias: str, scenario: str, tools: list[str] | None = None
    ) -> str:
        created = self._client.post(
            "/api/v1/agents",
            headers=self._headers(workspace),
            json={"name": alias, "alias": alias},
        )
        created.raise_for_status()
        agent = str(created.json()["id"])
        self._client.put(
            f"/api/v1/agents/{agent}/draft",
            headers=self._headers(workspace),
            json={
                "expected_revision": 1,
                "spec": {
                    "schema_version": 1,
                    "personality": "The restart drill's agent.",
                    "model_policy": {"provider": "deterministic", "scenario": scenario},
                    "tools": tools or [],
                    "limits": {
                        "max_execution_seconds": 900,
                        "max_elapsed_seconds": 86_400,
                        "max_model_calls": 20,
                        "max_tool_calls": 50,
                        "max_derived_retries": 3,
                    },
                },
            },
        ).raise_for_status()
        self._client.post(
            f"/api/v1/agents/{agent}/publish",
            headers=self._headers(workspace),
            json={"expected_revision": 2},
        ).raise_for_status()
        return agent

    def submit(self, workspace: str, agent: str, label: str) -> str:
        """Opens a Session and submits one Run into it.

        A Session per Run on purpose: Runs within one Session are serialized, so
        a drill that reused a Session would be timing the queue rather than the
        restart it just performed.
        """
        session = self._client.post(
            "/api/v1/sessions",
            headers=self._headers(workspace),
            json={"agent_id": agent, "session_mode": "persistent"},
        )
        session.raise_for_status()
        run = self._client.post(
            "/api/v1/runs",
            headers=self._headers(workspace, **{"Idempotency-Key": label}),
            json={"session_id": session.json()["id"], "input": "Restart drill."},
        )
        run.raise_for_status()
        return str(run.json()["id"])

    def read(self, workspace: str, run: str) -> dict[str, Any]:
        snapshot = self._client.get(f"/api/v1/runs/{run}", headers={"X-Workspace-Id": workspace})
        snapshot.raise_for_status()
        return dict(snapshot.json())

    def await_status(
        self, workspace: str, run: str, wanted: Sequence[str], timeout: float
    ) -> tuple[dict[str, Any], float]:
        """Waits for one of ``wanted``; answers the snapshot reached and how long it took."""
        started = time.monotonic()
        deadline = started + timeout
        snapshot: dict[str, Any] = {}
        while time.monotonic() < deadline:
            snapshot = self.read(workspace, run)
            status = snapshot["status"]
            if status in wanted:
                return snapshot, time.monotonic() - started
            if status in TERMINAL_STATUSES:
                raise SystemExit(
                    f"run {run} ended as {status!r}, "
                    f"waiting for one of {list(wanted)}"
                )
            time.sleep(0.1)
        raise SystemExit(
            f"run {run} stayed {snapshot.get('status')!r} for {timeout:.0f}s, "
            f"waiting for one of {list(wanted)}"
        )

    def events(self, workspace: str, run: str) -> list[Event]:
        """Reads the whole event history of a finished Run.

        Over the stream, because no route lists events: for a Run that has
        reached a terminal state the server delivers the history and closes, so
        the body ending is the history being complete.
        """
        frames: list[Event] = []
        with self._client.stream(
            "GET",
            f"/api/v1/runs/{run}/events",
            params={"workspace_id": workspace},
            timeout=RUN_TIMEOUT,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data:"):
                    payload = json.loads(line[len("data:") :])
                    frames.append(Event(int(payload["sequence"]), str(payload["event_type"])))
        return frames


def report(label: str, **facts: object) -> None:
    stated = "  ".join(f"{key}={value}" for key, value in facts.items())
    print(f"  {label:<22} {stated}")


def check(claim: bool, message: str) -> None:
    if not claim:
        raise SystemExit(f"FAILED: {message}")


def describe(events: Sequence[Event]) -> None:
    """Prints the shape of a history and insists it is one unbroken run of numbers."""
    sequences = [event.sequence for event in events]
    report(
        "events",
        count=len(events),
        contiguous=sequences == list(range(1, len(sequences) + 1)),
        leases=sum(1 for event in events if event.event_type == "run_lease_acquired"),
        last=events[-1].event_type if events else "-",
    )
    check(len(events) > 0, "the Run produced no events at all")
    # Numbered from one, nothing skipped and nothing repeated. A slice recorded
    # twice, or lost across a restart, shows up here and nowhere else.
    check(
        sequences == list(range(1, len(sequences) + 1)),
        f"the event sequence is not contiguous from 1: {sequences}",
    )


def worker_restart(console: Console, workspace: str, agent: str) -> None:
    """A Worker restarted mid-Run loses its slice, not the Run."""
    print("\n1. Worker restarted while executing")
    run = console.submit(workspace, agent, f"drill-worker-{time.time_ns()}")
    running, pickup = console.await_status(workspace, run, ["running"], RUN_TIMEOUT)
    interrupted_at = int(running["last_event_sequence"])
    report("picked up", run=run, seconds=f"{pickup:.2f}", sequence=interrupted_at)

    compose("restart", "worker")
    await_healthy("worker")
    finished, elapsed = console.await_status(
        workspace, run, ["completed", "failed"], RECOVERY_TIMEOUT
    )
    report("after restart", status=finished["status"], seconds=f"{elapsed:.2f}")
    check(finished["status"] == "completed", f"the Run ended {finished['status']!r}")

    events = console.events(workspace, run)
    describe(events)
    check(events[-1].event_type == "run_completed", "the history does not end in run_completed")
    # Work continued past the point the restart interrupted, rather than the Run
    # having already finished before the restart landed on it.
    check(
        events[-1].sequence > interrupted_at,
        f"the Run was already at #{interrupted_at} when the Worker restarted, so the "
        "restart proved nothing; raise DETERMINISTIC_MODEL_DELAY_MS and run it again",
    )


def redis_restart(console: Console, workspace: str, agent: str) -> None:
    """Redis buys latency, not correctness: the Run completes without it."""
    print("\n2. Redis stopped, then restarted")
    compose("stop", "redis")
    run = console.submit(workspace, agent, f"drill-redis-{time.time_ns()}")
    _, polled = console.await_status(workspace, run, ["running"], RECOVERY_TIMEOUT)
    finished, elapsed = console.await_status(workspace, run, ["completed", "failed"], RUN_TIMEOUT)
    report(
        "without redis",
        run=run,
        status=finished["status"],
        pickup=f"{polled:.2f}",
        seconds=f"{elapsed:.2f}",
    )
    check(
        finished["status"] == "completed",
        f"the Run ended {finished['status']!r} with Redis stopped",
    )
    describe(console.events(workspace, run))

    compose("start", "redis")
    await_healthy("redis")
    # Several Runs, and the best of them is the measurement. A wake-up is
    # published once and never repeated, so a Worker that is mid-claim when one
    # goes out simply misses it and waits a poll — the design says so, and the
    # first Run after Redis returns misses it almost every time while the
    # subscription is still being re-established. What the design does not allow
    # is the channel never delivering again, and that is what this rules out.
    pickups: list[float] = []
    for attempt in range(WAKE_UP_ATTEMPTS):
        woken = console.submit(workspace, agent, f"drill-redis-back-{time.time_ns()}")
        _, pickup = console.await_status(workspace, woken, ["running"], RECOVERY_TIMEOUT)
        finished, elapsed = console.await_status(
            workspace, woken, ["completed", "failed"], RUN_TIMEOUT
        )
        pickups.append(pickup)
        report(
            f"with redis #{attempt + 1}",
            run=woken,
            status=finished["status"],
            pickup=f"{pickup:.2f}",
            seconds=f"{elapsed:.2f}",
        )
        check(
            finished["status"] == "completed",
            f"the Run ended {finished['status']!r} after Redis returned",
        )
    check(
        min(pickups) < IDLE_POLL_SECONDS,
        f"no Run was picked up in under the {IDLE_POLL_SECONDS:.0f}s idle poll "
        f"({[f'{value:.2f}' for value in pickups]}), so the wake-up channel never "
        "recovered and the Worker is polling for everything",
    )


def scheduler_restart(console: Console, workspace: str, agent: str) -> None:
    """A Worker that dies holding a lease is recovered by the Scheduler, not by itself."""
    print("\n3. Worker killed holding a lease, Scheduler restarted")
    run = console.submit(workspace, agent, f"drill-scheduler-{time.time_ns()}")
    running, pickup = console.await_status(workspace, run, ["running"], RUN_TIMEOUT)
    report("picked up", run=run, seconds=f"{pickup:.2f}", sequence=running["last_event_sequence"])

    # SIGKILL, so the Worker cannot finish its slice, release its lease, or say
    # anything about the Run. The lease has to expire on its own.
    compose("kill", "worker")
    compose("restart", "scheduler")
    await_healthy("scheduler")
    recovered, waited = console.await_status(workspace, run, ["queued"], RECOVERY_TIMEOUT)
    report("lease expired", status=recovered["status"], seconds=f"{waited:.2f}")

    compose("start", "worker")
    await_healthy("worker")
    finished, elapsed = console.await_status(
        workspace, run, ["completed", "failed"], RECOVERY_TIMEOUT
    )
    report("worker returned", status=finished["status"], seconds=f"{elapsed:.2f}")
    check(finished["status"] == "completed", f"the recovered Run ended {finished['status']!r}")
    describe(console.events(workspace, run))


def containers_for_drill() -> set[str]:
    """Sandbox containers this platform currently has, by id.

    Read from the daemon rather than from the platform's own tables, because
    the failure this scenario exists to catch is exactly the one where the
    tables say a container is gone and the daemon disagrees.
    """
    found = subprocess.run(  # noqa: S603 - every argument is a literal from this file
        ["docker", "ps", "-aq", "--filter", "label=tiny-hermes.run"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if found.returncode != 0:
        detail = found.stderr.strip() or f"exit code {found.returncode}"
        raise SystemExit(f"docker ps failed while observing sandboxes: {detail}")
    return {line.strip() for line in found.stdout.splitlines() if line.strip()}


def await_sandbox_command(
    before: set[str], marker: str = "sleep 20", timeout: float = 30.0
) -> set[str]:
    """Wait until a new sandbox is running the command the model requested.

    A live container alone is not enough evidence: the Worker acquires it
    before calling the model, so killing at that point only interrupts model
    latency. `docker top` observes the process table from the daemon and proves
    the command crossed the Controller boundary before the Worker is killed.
    """
    deadline = time.monotonic() + timeout
    latest: set[str] = set()
    last_top_error = ""
    while time.monotonic() < deadline:
        latest = containers_for_drill() - before
        for container_id in latest:
            processes = subprocess.run(  # noqa: S603 - container id came from docker ps
                ["docker", "top", container_id, "-eo", "pid,args"],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
            if processes.returncode != 0:
                # A newly created container can briefly have no process table.
                # Keep looking, but preserve the daemon's explanation if the
                # command never becomes visible instead of reporting "0".
                last_top_error = processes.stderr.strip()
                continue
            if marker in processes.stdout:
                return latest
        time.sleep(0.2)
    detail = f"; last docker top error: {last_top_error}" if last_top_error else ""
    raise SystemExit(
        f"no new sandbox ran the expected command marker {marker!r} within {timeout:.0f}s; "
        f"containers seen: {len(latest)}{detail}"
    )


def sandbox_leak(console: Console, workspace: str) -> None:
    """A Worker killed with a container live, and nothing left behind.

    The other three scenarios prove no committed state is lost. This one proves
    no *container* is leaked, which is the failure phase 3B newly makes
    possible: a Worker that dies mid-command leaves a running container that
    only the Scheduler's cleanup authority can reclaim.
    """
    print("\n4. Worker killed with a sandbox live")
    if not os.environ.get("SANDBOX_IMAGE_DIGEST"):
        raise SystemExit(
            "FAILED: no approved sandbox image is configured; set "
            "SANDBOX_IMAGE_DIGEST before running the four-scenario drill"
        )

    before = containers_for_drill()
    agent = console.publish_agent(
        workspace, f"drill-tools-{time.time_ns()}", "shell_once", tools=["shell.exec"]
    )
    run = console.submit(workspace, agent, f"drill-sandbox-{time.time_ns()}")
    _, polled = console.await_status(workspace, run, ["running"], RECOVERY_TIMEOUT)
    started = await_sandbox_command(before)
    report("picked up", run=run, seconds=f"{polled:.2f}", containers=len(started))
    check(bool(started), "no sandbox container ran the requested command")

    compose("kill", "worker")
    compose("start", "worker")
    finished, elapsed = console.await_status(
        workspace, run, ["completed", "failed"], RECOVERY_TIMEOUT
    )
    report("after the kill", status=finished["status"], seconds=f"{elapsed:.2f}")
    # Completed, not merely finished. The Run has to get a *new* sandbox after
    # the Scheduler reclaims the old one — a `failed` here means the recovered
    # Run met its own abandoned reservation and gave up, which is how this
    # first behaved.
    check(
        finished["status"] == "completed",
        f"the recovered Run ended as {finished['status']} rather than completing",
    )

    events = console.events(workspace, run)
    describe(events)
    event_types = [event.event_type for event in events]
    try:
        interrupted_at = event_types.index("run_interrupted")
        recovered_at = event_types.index("run_recovery_approved", interrupted_at + 1)
        completed_at = event_types.index("run_completed", recovered_at + 1)
    except ValueError as error:
        raise SystemExit(
            "FAILED: sandbox recovery history did not contain interrupted, recovery, "
            f"and completion in order: {event_types}"
        ) from error
    report(
        "recovery events",
        interrupted=events[interrupted_at].sequence,
        recovered=events[recovered_at].sequence,
        completed=events[completed_at].sequence,
    )

    # The Scheduler reclaims on its own schedule, so this waits rather than
    # asserting immediately — the claim is that nothing is leaked, not that
    # nothing is ever briefly orphaned.
    deadline = time.monotonic() + RECOVERY_TIMEOUT
    leaked = containers_for_drill() - before
    while leaked and time.monotonic() < deadline:
        time.sleep(2.0)
        leaked = containers_for_drill() - before
    report("containers left", count=len(leaked))
    check(not leaked, f"{len(leaked)} sandbox container(s) outlived the Run")


def main() -> int:
    delay = int(os.environ.get("DETERMINISTIC_MODEL_DELAY_MS", "50"))
    if delay < MINIMUM_MODEL_DELAY_MS:
        raise SystemExit(
            "Bring the stack up with DETERMINISTIC_MODEL_DELAY_MS set to at least "
            f"{MINIMUM_MODEL_DELAY_MS}, and export the same value here. At {delay}ms a "
            "Run finishes before a restart can land inside it, and the drill would "
            "prove nothing."
        )
    print(f"Restart drill against {API}  (model delay {delay}ms)")
    started = time.monotonic()
    # This is an operator's local control-plane drill. A workstation-wide HTTP
    # proxy must not receive bootstrap/login requests for 127.0.0.1, and some
    # managed shells inject one without exposing proxy environment variables.
    with httpx.Client(base_url=API, timeout=30.0, trust_env=False) as client:
        console = Console(client)
        console.sign_in()
        workspace = console.create_workspace(f"Drill-{time.time_ns()}")
        agent = console.publish_agent(workspace, f"drill-{time.time_ns()}", "continue_once")
        report("workspace", id=workspace)
        report("agent", id=agent, scenario="continue_once")
        worker_restart(console, workspace, agent)
        redis_restart(console, workspace, agent)
        scheduler_restart(console, workspace, agent)
        sandbox_leak(console, workspace)
    print(f"\nAll four scenarios held. {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
