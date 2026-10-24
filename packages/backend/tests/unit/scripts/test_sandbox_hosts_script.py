"""The operator's script refuses a retire it was not told is safe."""

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[5] / "scripts"


def _load() -> Any:
    spec = spec_from_file_location(
        "tiny_hermes_sandbox_hosts_script", _SCRIPTS / "sandbox_hosts.py"
    )
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_retire_without_the_confirmation_does_nothing_and_says_why(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Retiring a host that is only cut off releases sandboxes that are still
    running: a Run with two. The flag is the operator saying it is gone."""
    script = _load()

    with pytest.raises(SystemExit) as stopped:
        script.main(["retire", "sbx-a"])

    assert stopped.value.code != 0
    assert "--confirm-host-is-gone" in capsys.readouterr().err
