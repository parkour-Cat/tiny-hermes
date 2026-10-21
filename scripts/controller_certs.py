"""Issue the private CA and the certificates for tier B's controller link.

Tier B design §4.2 and docs/operations.md §7. One CA for this link alone, and
one certificate per role: the controller's names the hosts callers dial; the
worker's and the scheduler's name none and can only be clients.

Usage::

    uv run --no-sync python scripts/controller_certs.py --out certs \\
        --host sandbox-1.internal --host 10.0.0.5
    # a year later, same CA, new pairs:
    uv run --no-sync python scripts/controller_certs.py --out certs \\
        --host sandbox-1.internal --host 10.0.0.5 --force

An existing CA in --out is kept, so a reissue changes only the pairs. Keep
`ca.key` off every host once the pairs are issued; nothing at runtime reads it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tiny_hermes.sandbox.transport.certs import issue, issue_ca, load_pair, write_pair
from tiny_hermes.sandbox.transport.tls import Role

#: The Compose service name the scheduler on the sandbox host dials
#: (`deploy/compose/compose.sandbox-host.yaml`).
COMPOSE_NAME = "controller"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Issue tier B's controller certificates.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        help="a name or address Workers dial the controller by; repeatable",
    )
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--force", action="store_true", help="replace existing pairs")
    options = parser.parse_args(argv)
    if not options.host:
        parser.error("--host: the controller's certificate must name what Workers dial")

    out: Path = options.out
    leaves = [out / f"{role.value}.{suffix}" for role in Role for suffix in ("pem", "key")]
    existing = [path for path in leaves if path.exists()]
    if existing and not options.force:
        raise SystemExit(f"{existing[0]} already exists; pass --force to reissue")

    if (out / "ca.pem").exists() and (out / "ca.key").exists():
        ca = load_pair(out / "ca.pem", out / "ca.key")
    else:
        ca = issue_ca()
        write_pair(out, "ca", ca)

    hosts = list(dict.fromkeys([*options.host, COMPOSE_NAME]))
    write_pair(
        out,
        Role.CONTROLLER.value,
        issue(ca, role=Role.CONTROLLER, hosts=hosts, days=options.days),
    )
    for caller in (Role.WORKER, Role.SCHEDULER):
        write_pair(out, caller.value, issue(ca, role=caller, days=options.days))
    print(f"issued in {out}: ca, controller ({', '.join(hosts)}), worker, scheduler")  # noqa: T201


if __name__ == "__main__":
    main(sys.argv[1:])
