"""List, drain, activate and retire sandbox hosts (tier C §4.8).

Usage, with `DATABASE_URL` pointing at the platform's database::

    uv run --no-sync python scripts/sandbox_hosts.py list
    uv run --no-sync python scripts/sandbox_hosts.py drain sbx-b
    uv run --no-sync python scripts/sandbox_hosts.py activate sbx-b
    uv run --no-sync python scripts/sandbox_hosts.py retire sbx-a --confirm-host-is-gone

`drain` stops new sandboxes landing on a host and says how many it still has;
stop it for maintenance when that reaches zero, then `activate` it.

`retire` is for a host that is gone and will not come back. It releases every
sandbox recorded on it, and their Runs move to other hosts and restore cold.
Never retire a host that is only unreachable: its sandboxes may still be
running, and a released one is a Run with two sandboxes writing one
workspace. Nothing retires a host on its own for exactly that reason.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from tiny_hermes.runs.application.sandbox_hosts import (
    UnknownSandboxHost,
    activate_host,
    drain_host,
    list_hosts,
    retire_host,
)
from tiny_hermes.shared.config import get_settings
from tiny_hermes.shared.database import build_session_factory


async def _run(arguments: argparse.Namespace) -> int:
    sessions = build_session_factory(get_settings())
    async with sessions() as session:
        try:
            if arguments.command == "list":
                now = datetime.now(UTC)
                for host in await list_hosts(session):
                    budget = "?" if host.memory_budget_mb is None else str(host.memory_budget_mb)
                    print(
                        f"{host.name:16} {host.state.value:9} "
                        f"seen {int((now - host.seen_at).total_seconds()):>5}s ago  "
                        f"{host.live_sandboxes:>4} sandboxes  "
                        f"{host.reserved_mb}/{budget} MB  {host.url or '-'}"
                    )
            elif arguments.command == "drain":
                left = await drain_host(session, arguments.name)
                print(f"{arguments.name} draining; {left} live sandboxes left")
            elif arguments.command == "activate":
                await activate_host(session, arguments.name)
                print(f"{arguments.name} active")
            else:
                released = await retire_host(session, arguments.name)
                print(f"{arguments.name} retired; {released} sandboxes released")
        except UnknownSandboxHost:
            print(f"no sandbox host named {arguments.name}", file=sys.stderr)
            return 1
        await session.commit()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    for command in ("drain", "activate"):
        commands.add_parser(command).add_argument("name")
    retire = commands.add_parser("retire")
    retire.add_argument("name")
    retire.add_argument(
        "--confirm-host-is-gone",
        action="store_true",
        help="the host is gone and will not come back; required",
    )
    arguments = parser.parse_args(argv)
    if arguments.command == "retire" and not arguments.confirm_host_is_gone:
        parser.error(
            "retire releases every sandbox on the host; if it is only unreachable they may "
            "still be running. Pass --confirm-host-is-gone once you know it is gone."
        )
    return asyncio.run(_run(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
