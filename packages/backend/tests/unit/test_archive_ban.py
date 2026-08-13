"""Design §14.3: trusted-process code never extracts an archive locally.

The scan reads tar streams and hashes them; the Docker daemon extracts inside
containers; nothing in this process may ever write an archive's members to a
filesystem it can reach. ruff's banned-api covers the importable names; this
test walks the source so a method call on an instance — which no import ban
can see — cannot dodge it either.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "packages" / "backend" / "src" / "tiny_hermes"

#: `.extractfile(` is fine — it returns a reader and touches no filesystem.
FORBIDDEN = re.compile(r"\.extractall\(|\.extract\(|unpack_archive\(")


def _sources() -> list[Path]:
    found = sorted(SRC.rglob("*.py"))
    assert found, "the walk found no sources; the ban would be vacuous"
    return found


def test_no_trusted_process_extracts_archives() -> None:
    offenders: list[str] = []
    for source in _sources():
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line):
                offenders.append(f"{source.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, "\n".join(
        ["archive extraction in trusted-process code:", *offenders]
    )


def test_minio_client_is_constructed_in_exactly_one_module() -> None:
    """One construction point, so credentials have one place to reach."""
    constructors = [
        source.relative_to(ROOT)
        for source in _sources()
        if re.search(r"^\s*from minio import Minio", source.read_text(encoding="utf-8"), re.M)
    ]
    assert [str(path).replace("\\", "/") for path in constructors] == [
        "packages/backend/src/tiny_hermes/session_workspace/infrastructure/minio_store.py"
    ]


def test_no_module_but_the_engine_and_the_helper_speaks_tar() -> None:
    """`tarfile` appears only where the design says bytes are (de)framed."""
    allowed = {
        # The engine scans streams and never extracts; the service builds tars
        # for restore; the port demuxes exports; all three are named in the
        # design's module boundaries (§5).
        "packages/backend/src/tiny_hermes/sandbox/infrastructure/docker_engine.py",
        "packages/backend/src/tiny_hermes/session_workspace/application/service.py",
        "packages/backend/src/tiny_hermes/session_workspace/infrastructure/sandbox_port.py",
    }
    importers = {
        str(source.relative_to(ROOT)).replace("\\", "/")
        for source in _sources()
        if re.search(r"^\s*import tarfile", source.read_text(encoding="utf-8"), re.M)
    }
    assert importers <= allowed, f"unexpected tar handling in: {sorted(importers - allowed)}"
