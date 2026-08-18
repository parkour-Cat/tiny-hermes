"""The egress proxy as a process of its own.

It exists separately for the reason the Sandbox Controller does: a boundary
that runs inside the thing it bounds is not a boundary. Product design §16.5 —
「M2 在引入 MCP 和 OpenAPI/HTTP 工具之前必须上线独立 `egress-proxy`，使 API、
Worker、sandbox-controller 和沙箱的动态出站目标经过网络级强制边界」.

What this process deliberately does not hold: the Docker socket, object-store
credentials, model keys. It decides where packets may go, and a process that
decides that must not also be worth attacking for something else.

It does hold a database connection, and reads only. Scopes come from the same
tables an administrator edits, so a change takes effect on the next connection
rather than on the next restart — and a sandbox's identity comes from the
address the Controller wrote down, because a container presents nothing.
"""

import asyncio
import contextlib
import logging
import signal
from ipaddress import ip_network

from tiny_hermes.egress.application.proxy import EgressProxy, ProxySettings
from tiny_hermes.egress.infrastructure.sql_directory import SqlScopeDirectory
from tiny_hermes.outbound.domain.address_policy import Network
from tiny_hermes.shared.config import Settings, get_settings
from tiny_hermes.shared.database import build_session_factory
from tiny_hermes.shared.logging import configure_logging

logger = logging.getLogger(__name__)


def main() -> None:
    configure_logging()
    asyncio.run(_serve())


async def _serve() -> None:
    settings = get_settings()
    if not settings.egress_proxy_token:
        # A proxy that accepts unauthenticated platform callers is a proxy any
        # process on the network can borrow. Refusing to start is louder than
        # accepting everything, and it fails where an operator is looking.
        raise SystemExit("EGRESS_PROXY_TOKEN is required to run the egress proxy")
    directory = SqlScopeDirectory(build_session_factory(settings))
    proxy = EgressProxy(
        directory,
        ProxySettings(
            token=settings.egress_proxy_token,
            approved_networks=_approved(settings),
            port=settings.egress_proxy_port,
        ),
    )
    stop = _stop_on_termination()
    logger.info(
        "egress proxy started: approved_networks=%s",
        len(_approved(settings)),
    )
    await proxy.serve(stop)
    logger.info("egress proxy stopped")


def _approved(settings: Settings) -> list[Network]:
    ranges: list[Network] = []
    for entry in settings.outbound_allowed_cidrs.split(","):
        cleaned = entry.strip()
        if not cleaned:
            continue
        try:
            ranges.append(ip_network(cleaned, strict=False))
        except ValueError:
            logger.warning("ignoring an approved range that is not a network: %s", cleaned)
    return ranges


def _stop_on_termination() -> asyncio.Event:
    """The same shape the other three processes use, for the same reason:
    SIGTERM has to reach the loop rather than the interpreter's default."""
    stop = asyncio.Event()
    running = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        received = getattr(signal, name, None)
        if received is None:
            continue
        with contextlib.suppress(NotImplementedError):
            running.add_signal_handler(received, stop.set)
    return stop
