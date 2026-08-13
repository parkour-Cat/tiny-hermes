"""Prove the session workspace through crashes, quotas, and tenants.

Run against a live Compose stack, through the public API only. The
`shell_from_input` deterministic scenario turns each Run's input into one
sandbox command and asserts its outcome itself, so this drill reads verdicts
from Run status instead of scraping transcripts.

Two phases, because the quota scenario needs a stack whose checkpoint quota is
small enough to cross quickly::

    docker compose -f deploy/compose/compose.yaml up -d --build --wait
    uv run --no-sync python scripts/workspace_drill.py

    WORKSPACE_MAX_BYTES=8388608 \\
      docker compose -f deploy/compose/compose.yaml up -d --wait
    uv run --no-sync python scripts/workspace_drill.py --phase quota

Like the restart drill, nothing here prints a credential, and nothing here
removes state.
"""

import json
import statistics
import subprocess  # noqa: S404 - the drill inspects and restarts containers
import sys
import time

import httpx
from restart_drill import (
    API,
    RECOVERY_TIMEOUT,
    RUN_TIMEOUT,
    Console,
    await_healthy,
    check,
    compose,
    report,
)

#: Design §16.4's gates are about the commit itself; over the public API the
#: drill can only time the whole Run, so the asserted bounds carry the model
#: round and queueing too. The raw numbers are printed for the verification
#: record; the assertions are the generous end-to-end envelopes.
SINGLE_COMMIT_RUNS = 12
SINGLE_COMMIT_P95_SECONDS = 10.0
LARGE_COMMIT_SECONDS = 60.0
NEXT_RUN_SECONDS = 15.0
WORKER_RSS_LIMIT_MB = 512.0

WRITE_MARKER = "workspace-persisted"


class WorkspaceConsole(Console):
    """The restart drill's console, plus input-carrying submissions."""

    def open_session(self, workspace: str, agent: str) -> str:
        session = self._client.post(
            "/api/v1/sessions",
            headers=self._headers(workspace),
            json={"agent_id": agent, "session_mode": "persistent"},
        )
        session.raise_for_status()
        return str(session.json()["id"])

    def run_command(
        self, workspace: str, session: str, command: str, label: str
    ) -> str:
        run = self._client.post(
            "/api/v1/runs",
            headers=self._headers(workspace, **{"Idempotency-Key": label}),
            json={"session_id": session, "input": command},
        )
        run.raise_for_status()
        return str(run.json()["id"])

    def resume(self, workspace: str, run: str) -> None:
        version = int(self.read(workspace, run)["state_version"])
        answer = self._client.post(
            f"/api/v1/runs/{run}/resume",
            headers=self._headers(workspace),
            json={"expected_state_version": version},
        )
        answer.raise_for_status()


def crash_persistence(console: WorkspaceConsole, workspace: str, agent: str) -> None:
    """Write in Run 1, kill the Worker mid-run, read it back in Run 2."""
    print("\n== a committed write survives a worker crash ==")
    session = console.open_session(workspace, agent)
    writing = console.run_command(
        workspace,
        session,
        f"printf '{WRITE_MARKER}' > drill.txt && cat drill.txt && sleep 8",
        "ws-crash-write",
    )
    console.await_status(workspace, writing, ("running",), RUN_TIMEOUT)
    compose("kill", "worker")
    compose("start", "worker")
    await_healthy("worker")
    snapshot, took = console.await_status(
        workspace, writing, ("completed",), RECOVERY_TIMEOUT + RUN_TIMEOUT
    )
    report("run 1", status=snapshot["status"], recovered_in=f"{took:.1f}s")

    reading = console.run_command(
        workspace, session, f"grep -q {WRITE_MARKER} drill.txt", "ws-crash-read"
    )
    verdict, _ = console.await_status(workspace, reading, ("completed", "failed"), RUN_TIMEOUT)
    report("run 2", status=verdict["status"])
    check(
        verdict["status"] == "completed",
        "the committed file did not survive into the next Run",
    )


def tenant_isolation(console: WorkspaceConsole, workspace: str, agent: str) -> None:
    """A second Session must see an empty workspace, not the first one's files."""
    print("\n== two sessions cannot see each other's files ==")
    other = console.open_session(workspace, agent)
    probing = console.run_command(
        workspace, other, "test ! -e drill.txt", "ws-isolation"
    )
    verdict, _ = console.await_status(workspace, probing, ("completed", "failed"), RUN_TIMEOUT)
    report("probe", status=verdict["status"])
    check(
        verdict["status"] == "completed",
        "a fresh Session saw another Session's file",
    )


def performance_gates(console: WorkspaceConsole, workspace: str, agent: str) -> None:
    """Design §16.4, measured end to end and printed for the record."""
    print("\n== performance gates ==")
    durations: list[float] = []
    for index in range(SINGLE_COMMIT_RUNS):
        session = console.open_session(workspace, agent)
        started = time.monotonic()
        run = console.run_command(
            workspace,
            session,
            "dd if=/dev/zero of=data.bin bs=1M count=1 2>/dev/null && sync",
            f"ws-perf-{index}",
        )
        console.await_status(workspace, run, ("completed",), RUN_TIMEOUT)
        durations.append(time.monotonic() - started)
    p95 = _percentile(durations, 95.0)
    report(
        "1MiB commits",
        runs=len(durations),
        p50=f"{_percentile(durations, 50.0):.2f}s",
        p95=f"{p95:.2f}s",
    )
    check(p95 <= SINGLE_COMMIT_P95_SECONDS, f"1MiB commit p95 {p95:.2f}s over budget")

    session = console.open_session(workspace, agent)
    started = time.monotonic()
    large = console.run_command(
        workspace,
        session,
        "mkdir -p tree && cd tree && "
        "for i in $(seq 1 500); do head -c 65536 /dev/urandom > f$i.bin; done && "
        "dd if=/dev/zero of=../bulk.bin bs=1M count=68 2>/dev/null",
        "ws-perf-large",
    )
    console.await_status(workspace, large, ("completed",), max(RUN_TIMEOUT, 300.0))
    large_took = time.monotonic() - started
    rss = _worker_rss_mb()
    report(
        "large commit",
        files=501,
        bytes="~100MiB",
        took=f"{large_took:.1f}s",
        worker_rss=f"{rss:.0f}MiB",
    )
    check(large_took <= LARGE_COMMIT_SECONDS, f"large commit took {large_took:.1f}s")
    check(rss <= WORKER_RSS_LIMIT_MB, f"worker RSS {rss:.0f}MiB breaks the streaming claim")

    started = time.monotonic()
    reader = console.run_command(
        workspace, session, "test -e bulk.bin && test -e tree/f500.bin", "ws-perf-next"
    )
    verdict, _ = console.await_status(workspace, reader, ("completed", "failed"), RUN_TIMEOUT)
    next_took = time.monotonic() - started
    report("next run", status=verdict["status"], took=f"{next_took:.1f}s")
    check(verdict["status"] == "completed", "the next Run did not see the large commit")
    check(next_took <= NEXT_RUN_SECONDS + LARGE_COMMIT_SECONDS, "next-Run availability over budget")


def quota_rollback(console: WorkspaceConsole, workspace: str, agent: str) -> None:
    """A1: only the over-limit step rolls back, and resume finds the old files."""
    print("\n== over-quota pauses honestly and resumes on the old revision ==")
    session = console.open_session(workspace, agent)
    keeper = console.run_command(
        workspace, session, "printf keep > keep.txt", "ws-quota-keep"
    )
    console.await_status(workspace, keeper, ("completed",), RUN_TIMEOUT)

    breaker = console.run_command(
        workspace,
        session,
        "dd if=/dev/zero of=big.bin bs=1M count=12 2>/dev/null",
        "ws-quota-break",
    )
    paused, took = console.await_status(
        workspace,
        breaker,
        ("paused", "completed", "failed", "interrupted"),
        RECOVERY_TIMEOUT,
    )
    if paused["status"] != "paused":
        # Before failing, say what the platform recorded: the event names are
        # the diagnosis, and reading them beats guessing.
        for event in console.events(workspace, breaker):
            report("event", sequence=event.sequence, type=event.event_type)
        check(False, f"the over-quota run ended {paused['status']!r}, not paused")
    report(
        "over-quota run",
        status=paused["status"],
        reason=paused.get("pause_reason"),
        took=f"{took:.1f}s",
    )
    check(paused.get("pause_reason") == "limit", "the pause does not name the limit")

    console.resume(workspace, breaker)
    resumed, _ = console.await_status(workspace, breaker, ("completed",), RUN_TIMEOUT)
    report("resumed run", status=resumed["status"])

    proving = console.run_command(
        workspace,
        session,
        "test -e keep.txt && test ! -e big.bin",
        "ws-quota-prove",
    )
    verdict, _ = console.await_status(workspace, proving, ("completed", "failed"), RUN_TIMEOUT)
    report("old revision", status=verdict["status"])
    check(
        verdict["status"] == "completed",
        "resume did not restore the preceding revision",
    )


def no_leftovers() -> None:
    """No labelled container or volume outlives the drill."""
    print("\n== nothing labelled outlives the drill ==")
    containers = _docker_lines(
        "ps", "-aq", "--filter", "label=tiny-hermes.run"
    )
    volumes = _docker_lines(
        "volume", "ls", "-q", "--filter", "label=tiny-hermes.run"
    )
    report("leftovers", containers=len(containers), volumes=len(volumes))
    check(not containers, f"sandbox containers left behind: {len(containers)}")
    check(not volumes, f"data volumes left behind: {len(volumes)}")


def _docker_lines(*arguments: str) -> list[str]:
    answer = subprocess.run(  # noqa: S603 - literals from this file
        ["docker", *arguments],  # noqa: S607 - docker resolves from PATH on purpose
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in answer.stdout.splitlines() if line.strip()]


def _worker_rss_mb() -> float:
    """The worker container's memory as Docker reports it, in MiB."""
    listed = _docker_lines(
        "ps", "--format", "{{.Names}}", "--filter", "name=worker"
    )
    if not listed:
        return 0.0
    stats = subprocess.run(  # noqa: S603 - container name from docker itself
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", listed[0]],  # noqa: S607

        check=True,
        capture_output=True,
        text=True,
    )
    usage = str(json.loads(stats.stdout)["MemUsage"]).split("/")[0].strip()
    return _to_mib(usage)


def _to_mib(usage: str) -> float:
    scales = {"KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0, "B": 1 / (1024 * 1024)}
    for suffix, scale in scales.items():
        if usage.endswith(suffix):
            return float(usage[: -len(suffix)]) * scale
    return float(usage)


def _percentile(values: list[float], wanted: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    quantiles = statistics.quantiles(ordered, n=100, method="inclusive")
    return quantiles[min(98, max(0, int(wanted) - 1))]


def main() -> None:
    phase = "quota" if "--phase" in sys.argv and "quota" in sys.argv else "main"
    with httpx.Client(base_url=API, timeout=30.0) as client:
        console = WorkspaceConsole(client)
        console.sign_in()
        workspace = console.create_workspace(f"workspace-drill-{phase}-{int(time.time())}")
        agent = console.publish_agent(
            workspace, f"ws-drill-{phase}", "shell_from_input", tools=["shell.exec"]
        )

        if phase == "quota":
            quota_rollback(console, workspace, agent)
        else:
            crash_persistence(console, workspace, agent)
            tenant_isolation(console, workspace, agent)
            performance_gates(console, workspace, agent)

    _await_reclamation()
    no_leftovers()
    print(f"\nworkspace drill ({phase}): PASS")


def _await_reclamation(timeout: float = 120.0) -> None:
    """Terminal Runs destroy instantly; a paused one waits for the Scheduler."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        containers = _docker_lines("ps", "-aq", "--filter", "label=tiny-hermes.run")
        volumes = _docker_lines("volume", "ls", "-q", "--filter", "label=tiny-hermes.run")
        if not containers and not volumes:
            return
        time.sleep(3)


if __name__ == "__main__":
    main()
