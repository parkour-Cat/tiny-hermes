"""Mutual TLS for the Controller's link, and the roles a certificate carries.

Tier B design §4.2. A certificate proves the same kind of fact that reaching
the Unix socket did — "this is one of the platform's own processes" — and no
more. Every call is still checked against its reservation and lease by
`application/controller.py`; this module does not replace that, and nothing
here may be read as if it did.

What a certificate adds is *which* process: the Subject's OU is `worker` or
`scheduler`. The Scheduler calls `cleanup` and nothing else, and over TLS that
is now enforced. Over the Unix socket there is no certificate and no role, and
the server serves every action as it always has.
"""

import os
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.x509.oid import NameOID

from tiny_hermes.sandbox.transport.actions import ACTIONS

#: When the startup log starts warning that a certificate is about to lapse.
EXPIRY_WARNING = timedelta(days=30)


class Role(StrEnum):
    CONTROLLER = "controller"
    WORKER = "worker"
    SCHEDULER = "scheduler"


_ALLOWED: dict[Role, frozenset[str]] = {
    Role.WORKER: ACTIONS,
    # Technical design §11.1's `sandbox.cleanup` authority. Before TLS it was a
    # name with no credential behind it; the Scheduler calls nothing else.
    Role.SCHEDULER: frozenset({"cleanup"}),
}


def allowed_actions(role: Role | None) -> frozenset[str]:
    """What a TLS caller may ask. No role, or the Controller's own, is nothing."""
    return _ALLOWED.get(role, frozenset()) if role is not None else frozenset()


def role_of(peer: Mapping[str, Any] | None) -> Role | None:
    """The OU of a peer certificate, as `SSLObject.getpeercert()` reports it."""
    if not peer:
        return None
    subject: Any = peer.get("subject", ())
    for relative_name in subject:
        for key, value in relative_name:
            if key == "organizationalUnitName":
                try:
                    return Role(value)
                except ValueError:
                    return None
    return None


@dataclass(frozen=True)
class TlsFiles:
    ca_file: str
    cert_file: str
    key_file: str


class IdentityError(Exception):
    """A process's own TLS material is unusable; it must not start."""


@dataclass(frozen=True)
class Identity:
    role: Role
    not_after: datetime
    expiring_soon: bool


def load_identity(files: TlsFiles, *, role: Role, now: datetime | None = None) -> Identity:
    """Check this process's certificate before it serves or calls anything.

    Each failure names the setting to change. A Worker holding the Scheduler's
    certificate would otherwise start and then be refused on every call but
    one — an outage that looks like a Controller fault.
    """
    for setting, path in (
        ("SANDBOX_CONTROLLER_CA_FILE", files.ca_file),
        ("SANDBOX_CONTROLLER_CERT_FILE", files.cert_file),
        ("SANDBOX_CONTROLLER_KEY_FILE", files.key_file),
    ):
        if not os.access(path, os.R_OK):
            raise IdentityError(f"{setting}={path} cannot be read")
    certificate = x509.load_pem_x509_certificate(Path(files.cert_file).read_bytes())
    units = certificate.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)
    held = str(units[0].value) if units else ""
    if held != role.value:
        raise IdentityError(
            f"SANDBOX_CONTROLLER_CERT_FILE={files.cert_file} is a {held or 'roleless'} "
            f"certificate; this process is the {role.value}"
        )
    moment = now or datetime.now(UTC)
    if moment >= certificate.not_valid_after_utc:
        raise IdentityError(
            f"SANDBOX_CONTROLLER_CERT_FILE={files.cert_file} expired "
            f"{certificate.not_valid_after_utc.isoformat()}"
        )
    if moment < certificate.not_valid_before_utc:
        raise IdentityError(
            f"SANDBOX_CONTROLLER_CERT_FILE={files.cert_file} is not valid until "
            f"{certificate.not_valid_before_utc.isoformat()}"
        )
    return Identity(
        role=role,
        not_after=certificate.not_valid_after_utc,
        expiring_soon=certificate.not_valid_after_utc - moment < EXPIRY_WARNING,
    )


def server_context(files: TlsFiles) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(files.cert_file, files.key_file)
    # Only this CA: a certificate any public CA signed proves nothing here.
    context.load_verify_locations(cafile=files.ca_file)
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def client_context(files: TlsFiles) -> ssl.SSLContext:
    """Verifies the Controller's name as well as its CA.

    Built once per process, so the files are read once. It does not make the
    handshakes cheaper: asyncio offers no way to hand a client connection a
    previous TLS session, so every call pays a full handshake. What that
    costs per round is measured, not assumed (design §4.4).
    """
    # `cafile` alone: create_default_context loads the system's CAs only when
    # given none of its own.
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=files.ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(files.cert_file, files.key_file)
    return context
