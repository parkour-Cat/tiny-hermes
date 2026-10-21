"""Issue the private CA and the certificates the Controller's TLS link uses.

Tier B design §4.2. In the package rather than only in the deployment script so
the integration tests handshake with exactly the certificates a deployment
gets: a test PKI built some other way would prove only that *it* works.

The role is the Subject's OU. The Controller's certificate is for serving and
names its hosts; a caller's is for clients only and names none, so a Worker's
key cannot stand up something other Workers would accept as the Controller.
"""

import ipaddress
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from tiny_hermes.sandbox.transport.tls import Role

#: Clock skew between hosts should not make a fresh certificate "not yet valid".
_BACKDATE = timedelta(minutes=5)


@dataclass(frozen=True)
class Issued:
    certificate: x509.Certificate
    key: ec.EllipticCurvePrivateKey


def issue_ca(*, days: int = 3650, now: datetime | None = None) -> Issued:
    start = (now or datetime.now(UTC)) - _BACKDATE
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tiny-hermes controller CA")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return Issued(certificate, key)


def issue(
    ca: Issued,
    *,
    role: Role,
    hosts: Sequence[str] = (),
    days: int = 365,
    now: datetime | None = None,
) -> Issued:
    if role is Role.CONTROLLER and not hosts:
        raise ValueError("the controller's certificate must name the hosts callers dial")
    if role is not Role.CONTROLLER and hosts:
        raise ValueError("a caller's certificate names no host")
    start = (now or datetime.now(UTC)) - _BACKDATE
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, role.value),
                    x509.NameAttribute(NameOID.COMMON_NAME, f"tiny-hermes {role.value}"),
                ]
            )
        )
        .issuer_name(ca.certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage(
                [
                    ExtendedKeyUsageOID.SERVER_AUTH
                    if role is Role.CONTROLLER
                    else ExtendedKeyUsageOID.CLIENT_AUTH
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.key.public_key()),
            critical=False,
        )
    )
    if hosts:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([_general_name(host) for host in hosts]),
            critical=False,
        )
    return Issued(builder.sign(ca.key, hashes.SHA256()), key)


def _general_name(host: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def write_pair(directory: Path, name: str, issued: Issued) -> None:
    """`<name>.pem` and `<name>.key`, the key readable by its owner alone."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.pem").write_bytes(
        issued.certificate.public_bytes(serialization.Encoding.PEM)
    )
    key_path = directory / f"{name}.key"
    # Created 0600 rather than chmod-ed after: there is no moment at which the
    # key exists with the umask's permissions.
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(
            issued.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )


def load_pair(certificate: Path, key: Path) -> Issued:
    """A pair `write_pair` wrote — how the script issues more from an existing CA."""
    loaded = serialization.load_pem_private_key(key.read_bytes(), password=None)
    if not isinstance(loaded, ec.EllipticCurvePrivateKey):
        raise ValueError(f"{key}: not a key this module issued")
    return Issued(x509.load_pem_x509_certificate(certificate.read_bytes()), loaded)
