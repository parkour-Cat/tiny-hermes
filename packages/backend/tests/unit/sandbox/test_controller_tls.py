"""Who may call the Controller over TLS, decided from real certificates.

The certificates are issued by `certs.py` — the same code the deployment script
uses — and read back with `cryptography`, not built as dicts. A dict in the
shape we expect `getpeercert()` to return would prove only that we can build
that dict.
"""

import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID
from tiny_hermes.sandbox.transport.certs import issue, issue_ca, write_pair
from tiny_hermes.sandbox.transport.server import ACTIONS
from tiny_hermes.sandbox.transport.tls import (
    IdentityError,
    Role,
    TlsFiles,
    allowed_actions,
    client_context,
    load_identity,
    role_of,
    server_context,
)

NOW = datetime(2026, 10, 21, tzinfo=UTC)


def _files(directory: Path, role: Role, *, days: int = 365) -> TlsFiles:
    ca = issue_ca(now=NOW)
    write_pair(directory, "ca", ca)
    hosts = ["sandbox-1.internal"] if role is Role.CONTROLLER else []
    write_pair(directory, role.value, issue(ca, role=role, hosts=hosts, days=days, now=NOW))
    return TlsFiles(
        ca_file=str(directory / "ca.pem"),
        cert_file=str(directory / f"{role.value}.pem"),
        key_file=str(directory / f"{role.value}.key"),
    )


def test_a_worker_may_call_everything_the_controller_answers() -> None:
    assert allowed_actions(Role.WORKER) == ACTIONS


def test_the_scheduler_may_only_clean_up() -> None:
    # Technical design §11.1's `sandbox.cleanup` authority, finally carried by
    # a credential: the Scheduler calls nothing else.
    assert allowed_actions(Role.SCHEDULER) == frozenset({"cleanup"})


@pytest.mark.parametrize("role", [Role.CONTROLLER, None])
def test_a_certificate_with_no_caller_role_may_call_nothing(role: Role | None) -> None:
    assert allowed_actions(role) == frozenset()


def test_the_role_is_read_from_what_the_handshake_reports(tmp_path: Path) -> None:
    """`getpeercert()`'s own shape, produced by `ssl` from our certificate."""
    files = _files(tmp_path, Role.SCHEDULER)
    # `_test_decode_cert` is how `ssl` itself turns a PEM file into the dict
    # `getpeercert()` returns; it is the one way to get that shape without a
    # handshake, and the integration tests cover the handshake.
    decoded = ssl._ssl._test_decode_cert(files.cert_file)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]  # noqa: SLF001

    assert role_of(decoded) is Role.SCHEDULER  # pyright: ignore[reportUnknownArgumentType]


@pytest.mark.parametrize("peer", [None, {}, {"subject": ((("commonName", "x"),),)}])
def test_a_peer_without_a_role_has_none(peer: object) -> None:
    assert role_of(peer) is None  # pyright: ignore[reportArgumentType]


def test_an_identity_loads_when_its_role_matches(tmp_path: Path) -> None:
    identity = load_identity(_files(tmp_path, Role.WORKER), role=Role.WORKER, now=NOW)

    assert identity.role is Role.WORKER
    assert not identity.expiring_soon


def test_a_worker_holding_the_scheduler_certificate_will_not_start(tmp_path: Path) -> None:
    # It would start, and then every call it made but cleanup would be refused.
    with pytest.raises(IdentityError, match="scheduler"):
        load_identity(_files(tmp_path, Role.SCHEDULER), role=Role.WORKER, now=NOW)


def test_an_expired_certificate_will_not_start(tmp_path: Path) -> None:
    files = _files(tmp_path, Role.WORKER, days=30)

    with pytest.raises(IdentityError, match="expired"):
        load_identity(files, role=Role.WORKER, now=NOW + timedelta(days=31))


def test_a_certificate_inside_its_last_thirty_days_says_so(tmp_path: Path) -> None:
    files = _files(tmp_path, Role.WORKER, days=60)

    identity = load_identity(files, role=Role.WORKER, now=NOW + timedelta(days=45))

    assert identity.expiring_soon


@pytest.mark.parametrize("which", ["ca_file", "cert_file", "key_file"])
def test_a_missing_file_is_named(tmp_path: Path, which: str) -> None:
    files = _files(tmp_path, Role.WORKER)
    Path(getattr(files, which)).unlink()

    with pytest.raises(IdentityError, match=which.upper()):
        load_identity(files, role=Role.WORKER, now=NOW)


def test_the_controller_certificate_names_its_host_and_serves(tmp_path: Path) -> None:
    files = _files(tmp_path, Role.CONTROLLER)
    certificate = x509.load_pem_x509_certificate(Path(files.cert_file).read_bytes())

    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert names.value.get_values_for_type(x509.DNSName) == ["sandbox-1.internal"]
    assert list(usage.value) == [ExtendedKeyUsageOID.SERVER_AUTH]


def test_a_caller_certificate_can_only_be_a_client(tmp_path: Path) -> None:
    # So a Worker's certificate cannot stand in for the Controller's.
    files = _files(tmp_path, Role.WORKER)
    certificate = x509.load_pem_x509_certificate(Path(files.cert_file).read_bytes())

    usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert list(usage.value) == [ExtendedKeyUsageOID.CLIENT_AUTH]


def test_private_keys_are_written_for_their_owner_only(tmp_path: Path) -> None:
    files = _files(tmp_path, Role.WORKER)

    assert Path(files.key_file).stat().st_mode & 0o077 == 0


def test_both_contexts_demand_a_certificate_from_the_other_side(tmp_path: Path) -> None:
    server = server_context(_files(tmp_path / "s", Role.CONTROLLER))
    client = client_context(_files(tmp_path / "c", Role.WORKER))

    assert server.verify_mode is ssl.CERT_REQUIRED
    assert client.verify_mode is ssl.CERT_REQUIRED
    assert client.check_hostname
