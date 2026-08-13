"""Generated local secrets must be random, 32-byte KEK, and not the compose zeroes."""

from __future__ import annotations

import base64
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
import yaml

_SPEC = spec_from_file_location(
    "tiny_hermes_generate_local_secrets",
    Path(__file__).parents[5] / "scripts" / "generate_local_secrets.py",
)
assert _SPEC is not None and _SPEC.loader is not None
generate = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = generate
_SPEC.loader.exec_module(generate)

COMPOSE = Path(__file__).resolve().parents[5] / "deploy" / "compose" / "compose.yaml"
ZERO_KEK = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def test_minted_secrets_meet_the_documented_lengths() -> None:
    minted = generate.mint()
    assert len(minted["SESSION_COOKIE_SECRET"]) >= 32
    assert len(minted["BOOTSTRAP_TOKEN"]) >= 32
    kek = base64.b64decode(minted["TINY_HERMES_KEK"], validate=True)
    assert len(kek) == 32
    assert minted["TINY_HERMES_KEK"] != ZERO_KEK
    assert minted["SESSION_COOKIE_SECRET"] != minted["BOOTSTRAP_TOKEN"]
    assert minted["TINY_HERMES_KEK_ID"] == "v1"


def test_a_second_mint_is_not_the_first() -> None:
    first = generate.mint()
    second = generate.mint()
    assert first["TINY_HERMES_KEK"] != second["TINY_HERMES_KEK"]
    assert first["SESSION_COOKIE_SECRET"] != second["SESSION_COOKIE_SECRET"]


def test_writing_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    dest = tmp_path / ".env"
    dest.write_text("SESSION_COOKIE_SECRET=already-there\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="already exists"):
        generate.write_env(dest, generate.mint(), force=False)


def test_force_replaces_only_the_secret_keys(tmp_path: Path) -> None:
    dest = tmp_path / ".env"
    dest.write_text("ENVIRONMENT=development\nSESSION_COOKIE_SECRET=old\n", encoding="utf-8")
    minted = generate.mint()
    generate.write_env(dest, minted, force=True)
    text = dest.read_text(encoding="utf-8")
    assert "ENVIRONMENT=development" in text
    assert minted["SESSION_COOKIE_SECRET"] in text
    assert "SESSION_COOKIE_SECRET=old" not in text


def test_compose_config_puts_the_generated_kek_on_the_api_not_the_controller(
    tmp_path: Path,
) -> None:
    dest = tmp_path / ".env"
    minted = generate.mint()
    generate.write_env(dest, minted, force=True)
    rendered = generate.compose_config(dest)
    document = yaml.safe_load(rendered)
    api_env = document["services"]["api"]["environment"]
    controller_env = document["services"]["controller"]["environment"]
    assert api_env["TINY_HERMES_KEK"] == minted["TINY_HERMES_KEK"]
    assert "TINY_HERMES_KEK" not in controller_env
    assert COMPOSE.is_file()
