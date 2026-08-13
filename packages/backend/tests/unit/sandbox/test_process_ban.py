"""The ban that keeps "no host-execution fallback" true after this slice ends.

Product design §16: `沙箱失败时不得回退宿主机执行`. That is easy to honour while
writing the sandbox and easy to break six months later, when somebody handling a
`DockerUnavailable` reaches for `subprocess.run` because it is right there and
the test they are fixing goes green. This makes the linter refuse.

The Docker client is banned by the same rule for the same reason: exactly one
module may hold the handle to the daemon, and a second one appearing is not a
thing to notice in review.
"""

import subprocess  # noqa: S404 - running the linter is the assertion
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
SRC = ROOT / "packages" / "backend" / "src" / "tiny_hermes"

BANNED = [
    "import subprocess\n\n\ndef go() -> None:\n    subprocess.run(['ls'])\n",
    "import subprocess\n\n\ndef go() -> None:\n    subprocess.Popen(['ls'])\n",
    "import os\n\n\ndef go() -> None:\n    os.system('ls')\n",
    "import asyncio\n\n\nasync def go() -> None:\n"
    "    await asyncio.create_subprocess_exec('ls')\n",
    "import asyncio\n\n\nasync def go() -> None:\n"
    "    await asyncio.create_subprocess_shell('ls')\n",
    "import docker\n\n\ndef go() -> None:\n    docker.from_env()\n",
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
def test_ruff_refuses_starting_a_process_outside_the_sandbox_module(source: str) -> None:
    """Written under the real source tree so the repository's own rules apply."""
    target = SRC / "_process_ban_probe.py"
    target.write_text(source, encoding="utf-8")
    try:
        result = _ruff(target)
    finally:
        target.unlink()
    assert result.returncode != 0, result.stdout
    assert "TID251" in result.stdout


def test_the_sandbox_module_lints_clean() -> None:
    """The exemption is real, so the guarded path is not merely unlinted by luck."""
    result = _ruff(SRC / "sandbox")
    assert result.returncode == 0, result.stdout


def test_no_module_under_source_starts_a_process() -> None:
    """The state of the tree, not only the rule.

    A rule proves what a new file cannot do. This proves what the existing ones
    do not, which is the fact somebody reading a review actually wants.
    """
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in SRC.rglob("*.py")
        if any(
            marker in path.read_text(encoding="utf-8")
            for marker in ("subprocess.", "os.system(", "os.popen(", "create_subprocess")
        )
    ]
    assert offenders == []


def test_the_sandbox_module_does_not_build_an_http_client() -> None:
    """TID251 is one code, so the two bans share an exemption list.

    `sandbox/infrastructure` is exempt so it can hold the Docker handle, and
    that exemption also lifts the HTTP ban there. This turns the coupling into
    an asserted fact rather than a thing that happens to be fine today.
    """
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (SRC / "sandbox").rglob("*.py")
        if "httpx" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_the_outbound_module_does_not_start_a_process() -> None:
    """The other half of the same coupling, in the other direction."""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (SRC / "outbound").rglob("*.py")
        if any(
            marker in path.read_text(encoding="utf-8")
            for marker in ("subprocess", "os.system", "create_subprocess", "docker")
        )
    ]
    assert offenders == []


def test_only_one_module_may_hold_the_docker_handle() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ignores = config["tool"]["ruff"]["lint"]["per-file-ignores"]
    exempted = {path for path, codes in ignores.items() if "TID251" in codes}
    assert "packages/backend/src/tiny_hermes/sandbox/infrastructure/**/*.py" in exempted
