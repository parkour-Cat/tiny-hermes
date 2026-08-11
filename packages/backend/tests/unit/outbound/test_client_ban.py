"""The ban on building a raw HTTP client anywhere but the outbound module.

A lint rule that is configured and does not bite is worse than no rule, because
it reads like a control in a review. So one test runs ruff and watches it fail,
and another pins the exemption list — widening it then shows up as a failing
test rather than as a line in a diff nobody reads twice.
"""

import subprocess  # noqa: S404 - running the linter is the assertion
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]

#: The only places allowed to construct a client or a socket. `outbound` is the
#: guarded path itself; the other two are clients *of* this platform rather than
#: parts of it, and neither ships.
EXEMPT = {
    "packages/backend/src/tiny_hermes/outbound/**/*.py",
    "packages/backend/tests/**/*.py",
    "scripts/**/*.py",
}

BANNED = [
    "import httpx\n\n\ndef go() -> None:\n    httpx.AsyncClient()\n",
    "import httpx\n\n\ndef go() -> None:\n    httpx.Client()\n",
    "import socket\n\n\ndef go() -> None:\n    socket.socket()\n",
    "import urllib.request\n\n\ndef go() -> None:\n    urllib.request.urlopen('http://x')\n",
]


def _ruff(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - arguments are literals from this file
        [sys.executable, "-m", "ruff", "check", "--no-cache", str(target)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


@pytest.mark.parametrize("source", BANNED)
def test_ruff_refuses_a_raw_client_outside_the_outbound_module(
    source: str, tmp_path: Path
) -> None:
    """Written under `packages/backend/src` so the repository's own rules apply.

    A file in a temporary directory elsewhere would be linted with defaults and
    would prove nothing about this repository's configuration.
    """
    target = ROOT / "packages" / "backend" / "src" / "tiny_hermes" / "_ban_probe.py"
    del tmp_path
    target.write_text(source, encoding="utf-8")
    try:
        result = _ruff(target)
    finally:
        target.unlink()
    assert result.returncode != 0
    assert "TID251" in result.stdout


def test_the_outbound_module_is_allowed_to() -> None:
    """The exemption is real, so the guarded path is not merely unlinted by luck."""
    result = _ruff(ROOT / "packages" / "backend" / "src" / "tiny_hermes" / "outbound")
    assert result.returncode == 0, result.stdout


def test_the_exemption_list_is_exactly_three_paths() -> None:
    """Pinned, because quietly adding a fourth is how a ban stops meaning anything."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ignores = config["tool"]["ruff"]["lint"]["per-file-ignores"]
    exempted = {path for path, codes in ignores.items() if "TID251" in codes}
    assert exempted == EXEMPT
