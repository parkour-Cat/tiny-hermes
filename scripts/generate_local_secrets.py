"""Mint the three local Compose secrets that must not stay as documented zeroes.

The Compose defaults are for an isolated machine that has not generated
anything yet. An operator who is about to keep data should replace
``SESSION_COOKIE_SECRET``, ``BOOTSTRAP_TOKEN``, and ``TINY_HERMES_KEK``
before the first bootstrap.

Usage::

    uv run --no-sync python scripts/generate_local_secrets.py --env-file .env
    uv run --no-sync python scripts/generate_local_secrets.py --env-file .env --force
    uv run --no-sync python scripts/generate_local_secrets.py --stdout
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import subprocess  # noqa: S404 - compose config is this script's interpolation check
import sys
from pathlib import Path

COMPOSE_FILE = Path("deploy/compose/compose.yaml")
EXAMPLE_FILE = Path(".env.example")
MANAGED = (
    "SESSION_COOKIE_SECRET",
    "BOOTSTRAP_TOKEN",
    "TINY_HERMES_KEK",
    "TINY_HERMES_KEK_ID",
)


def mint() -> dict[str, str]:
    return {
        "SESSION_COOKIE_SECRET": secrets.token_urlsafe(32),
        "BOOTSTRAP_TOKEN": secrets.token_urlsafe(32),
        "TINY_HERMES_KEK": base64.b64encode(os.urandom(32)).decode("ascii"),
        "TINY_HERMES_KEK_ID": "v1",
    }


def write_env(dest: Path, minted: dict[str, str], *, force: bool) -> None:
    if dest.exists() and not force:
        raise SystemExit(f"{dest} already exists; pass --force to replace the secret keys")
    lines = _read_lines(dest)
    replaced: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = ""
        stripped = line.lstrip()
        if "=" in line and not stripped.startswith("#"):
            key = line.split("=", 1)[0].strip()
        if key in minted:
            out.append(f"{key}={minted[key]}\n")
            replaced.add(key)
        else:
            out.append(line if line.endswith("\n") else f"{line}\n")
    for key in MANAGED:
        if key not in replaced:
            out.append(f"{key}={minted[key]}\n")
    dest.write_text("".join(out), encoding="utf-8")


def _read_lines(dest: Path) -> list[str]:
    if dest.exists():
        return dest.read_text(encoding="utf-8").splitlines(keepends=True)
    if EXAMPLE_FILE.exists():
        return EXAMPLE_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    return []


def compose_config(env_file: Path) -> str:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(COMPOSE_FILE),
        "config",
    ]
    result = subprocess.run(  # noqa: S603 - arguments are literals plus the env path
        command, check=True, capture_output=True, text=True
    )
    return result.stdout


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)
    minted = mint()
    if args.stdout:
        for key in MANAGED:
            print(f"{key}={minted[key]}")
        return
    write_env(args.env_file, minted, force=args.force)
    print(f"wrote {', '.join(MANAGED)} to {args.env_file}")


if __name__ == "__main__":
    main(sys.argv[1:])
