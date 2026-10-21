"""The script an operator runs to issue tier B's controller certificates."""

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from cryptography import x509

_SPEC = spec_from_file_location(
    "tiny_hermes_controller_certs",
    Path(__file__).parents[5] / "scripts" / "controller_certs.py",
)
assert _SPEC is not None and _SPEC.loader is not None
certs = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = certs
_SPEC.loader.exec_module(certs)

ROLES = ("controller", "worker", "scheduler")


def _certificate(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def test_a_first_run_issues_a_ca_and_one_pair_per_role(tmp_path: Path) -> None:
    certs.main(["--out", str(tmp_path), "--host", "sandbox-1.internal", "--host", "10.0.0.5"])

    ca = _certificate(tmp_path / "ca.pem")
    for role in ROLES:
        assert _certificate(tmp_path / f"{role}.pem").issuer == ca.subject
        assert (tmp_path / f"{role}.key").stat().st_mode & 0o077 == 0
    assert (tmp_path / "ca.key").stat().st_mode & 0o077 == 0


def test_the_controller_answers_to_its_hosts_and_to_its_compose_name(tmp_path: Path) -> None:
    # Workers elsewhere dial the host; the scheduler beside it dials the
    # Compose service, `controller` (compose.sandbox-host.yaml).
    certs.main(["--out", str(tmp_path), "--host", "sandbox-1.internal", "--host", "10.0.0.5"])

    names = _certificate(tmp_path / "controller.pem").extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    )
    assert sorted(names.value.get_values_for_type(x509.DNSName)) == [
        "controller",
        "sandbox-1.internal",
    ]
    assert [str(ip) for ip in names.value.get_values_for_type(x509.IPAddress)] == ["10.0.0.5"]


def test_a_second_run_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    certs.main(["--out", str(tmp_path), "--host", "sandbox-1.internal"])

    with pytest.raises(SystemExit, match="already exists"):
        certs.main(["--out", str(tmp_path), "--host", "sandbox-1.internal"])


def test_reissuing_keeps_the_ca_so_hosts_need_only_their_new_pair(tmp_path: Path) -> None:
    certs.main(["--out", str(tmp_path), "--host", "sandbox-1.internal"])
    ca_before = (tmp_path / "ca.pem").read_bytes()
    worker_before = (tmp_path / "worker.pem").read_bytes()

    certs.main(["--out", str(tmp_path), "--host", "sandbox-1.internal", "--force"])

    assert (tmp_path / "ca.pem").read_bytes() == ca_before
    assert (tmp_path / "worker.pem").read_bytes() != worker_before


def test_a_controller_with_no_host_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        certs.main(["--out", str(tmp_path)])
