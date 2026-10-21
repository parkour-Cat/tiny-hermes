"""From Settings to the two ends of the Controller's link (tier B §4.1, §4.2).

The one place a process turns its configuration into a server or a client, so
the Worker, the Scheduler and the Controller cannot each read the TLS settings
a slightly different way. Each end checks its own certificate before it serves
or calls anything: a process with the wrong one stops at startup, with the
setting to change in the message, instead of failing on every call.
"""

import logging
import ssl

from tiny_hermes.sandbox.transport.address import ControllerAddress
from tiny_hermes.sandbox.transport.client import ControllerClient
from tiny_hermes.sandbox.transport.tls import (
    Role,
    TlsFiles,
    client_context,
    load_identity,
    server_context,
)
from tiny_hermes.shared.config import Settings

logger = logging.getLogger(__name__)


def controller_client(settings: Settings, *, role: Role) -> ControllerClient:
    address = settings.controller_address
    if not address.is_tls:
        return ControllerClient(address)
    files = _files(settings)
    _check(files, role)
    return ControllerClient(address, tls=client_context(files))


def controller_listener(settings: Settings) -> tuple[ControllerAddress, ssl.SSLContext | None]:
    address = settings.controller_listen_address
    if not address.is_tls:
        return address, None
    files = _files(settings)
    _check(files, Role.CONTROLLER)
    return address, server_context(files)


def _files(settings: Settings) -> TlsFiles:
    files = settings.controller_tls_files
    # Settings refuses a tls:// address without all three; this is that
    # guarantee restated for the type checker, not a second rule.
    assert files is not None, "Settings admitted tls:// without its files"  # noqa: S101
    return files


def _check(files: TlsFiles, role: Role) -> None:
    identity = load_identity(files, role=role)
    if identity.expiring_soon:
        logger.warning(
            "controller certificate expires %s; issue a new one and restart",
            identity.not_after.isoformat(),
            extra={"cert_file": files.cert_file, "role": role.value},
        )
