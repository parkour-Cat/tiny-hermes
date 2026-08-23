"""Rewrap, then destroy the old key — and check what that costs, both ways.

§376: 旧 KEK 在全部 Secret 重包、校验和审计完成前不能删除. Every test in this
repository keeps both keys alive for its whole life, so the moment the rule
is actually about — one key ceasing to exist — had never been rehearsed.

Two runs, and the second is the one worth having:

1. **Rewrap everything, then destroy.** Every secret must still open. This
   is the outcome an operator is trying to reach.
2. **Destroy with one secret left unrewrapped.** That secret must be
   permanently unreadable, and the drill has to *demonstrate* that rather
   than assert it is impossible — a rule nobody has watched fail is a rule
   people work around at 3am.

Destruction is modelled the way it actually happens: the service is rebuilt
holding only the new key, with no `previous` to fall back to. That is what
deleting a KEK from a deployment leaves behind, and it is the honest
simulation — nothing is deleted from disk here, so the drill cannot destroy
anything that was not created by it.

Runs entirely on a scratch database it creates. Never touches DATABASE_URL.

Usage::

    docker run -d --name th-drill-pg -e POSTGRES_USER=tiny_hermes \\
      -e POSTGRES_PASSWORD=local-only -e POSTGRES_DB=postgres \\
      -p 127.0.0.1:55433:5432 postgres:16
    uv run --no-sync python scripts/kek_destruction_drill.py \\
      --admin postgresql://tiny_hermes:local-only@127.0.0.1:55433/postgres
"""

import argparse
import asyncio
import json
import os
import subprocess  # noqa: S404 - driving alembic is part of the drill
import sys
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tiny_hermes.secrets.domain.envelope import (
    Envelope,
    UnwrapFailed,
    decode_kek,
    seal,
    unseal,
)

OLD_KEK = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
NEW_KEK = "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI="

SECRETS = ("alpha", "beta", "gamma")


class DrillFailed(RuntimeError):
    pass


def _alembic(database_url: str, *args: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["uv", "run", "--no-sync", "alembic", *args],  # noqa: S607 - uv is on PATH by design
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise DrillFailed(f"alembic {' '.join(args)} failed")


async def _rebuild(admin_url: str, name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


async def _seed(url: str, workspace_id: UUID) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, status, created_at)"
                    " VALUES (:w, 'Drill', 'active', now())"
                ),
                {"w": workspace_id},
            )
            for name in SECRETS:
                envelope = seal(f"value-of-{name}".encode(), decode_kek(OLD_KEK), "v1")
                await connection.execute(
                    text(
                        "INSERT INTO secrets (id, workspace_id, name, scope, status,"
                        " ciphertext, nonce, wrapped_dek, wrap_nonce, key_id, mask,"
                        " created_at, updated_at)"
                        " VALUES (:i, :w, :n, 'workspace', 'active', :c, :no, :d, :wn,"
                        " 'v1', 'v-***', now(), now())"
                    ),
                    {
                        "i": uuid4(),
                        "w": workspace_id,
                        "n": name,
                        "c": envelope.ciphertext,
                        "no": envelope.nonce,
                        "d": envelope.wrapped_dek,
                        "wn": envelope.wrap_nonce,
                    },
                )
    finally:
        await engine.dispose()


async def _rewrap_all_but(url: str, skip: str | None) -> int:
    """Rewrap every secret except one, so the drill can also show the failure.

    Deliberately not calling `SecretService.rewrap`: that method rewraps
    everything, and what the second run needs is the half-finished state an
    interrupted rotation leaves behind.
    """
    engine = create_async_engine(url)
    rewrapped = 0
    try:
        async with engine.begin() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT id, name, ciphertext, nonce, wrapped_dek, wrap_nonce,"
                        " key_id FROM secrets"
                    )
                )
            ).all()
            for row in rows:
                if skip is not None and row.name == skip:
                    continue
                from tiny_hermes.secrets.domain.envelope import (  # noqa: PLC0415
                    rewrap,
                )

                moved = rewrap(
                    Envelope(
                        ciphertext=bytes(row.ciphertext),
                        nonce=bytes(row.nonce),
                        wrapped_dek=bytes(row.wrapped_dek),
                        wrap_nonce=bytes(row.wrap_nonce),
                        key_id=str(row.key_id),
                    ),
                    decode_kek(OLD_KEK),
                    decode_kek(NEW_KEK),
                    "v2",
                )
                await connection.execute(
                    text(
                        "UPDATE secrets SET wrapped_dek = :d, wrap_nonce = :wn,"
                        " key_id = 'v2' WHERE id = :i"
                    ),
                    {"d": moved.wrapped_dek, "wn": moved.wrap_nonce, "i": row.id},
                )
                rewrapped += 1
    finally:
        await engine.dispose()
    return rewrapped


async def _readable_with_only(url: str, kek: str) -> dict[str, bool]:
    """Every secret, opened with the one key this deployment still holds.

    This is what "the old KEK was destroyed" means in practice: nothing to
    fall back to. No file is deleted anywhere — the drill cannot destroy
    something it did not create, and does not need to in order to measure
    the consequence.
    """
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT name, ciphertext, nonce, wrapped_dek, wrap_nonce,"
                        " key_id FROM secrets ORDER BY name"
                    )
                )
            ).all()
        answer: dict[str, bool] = {}
        for row in rows:
            envelope = Envelope(
                ciphertext=bytes(row.ciphertext),
                nonce=bytes(row.nonce),
                wrapped_dek=bytes(row.wrapped_dek),
                wrap_nonce=bytes(row.wrap_nonce),
                key_id=str(row.key_id),
            )
            try:
                answer[str(row.name)] = (
                    unseal(envelope, decode_kek(kek)) == f"value-of-{row.name}".encode()
                )
            except UnwrapFailed:
                answer[str(row.name)] = False
        return answer
    finally:
        await engine.dispose()


async def _one_run(admin_url: str, scratch: str, skip: str | None) -> dict[str, Any]:
    await _rebuild(admin_url, scratch)
    url = f"{admin_url.rsplit('/', 1)[0]}/{scratch}"
    _alembic(url, "upgrade", "head")
    workspace_id = uuid4()
    await _seed(url, workspace_id)
    rewrapped = await _rewrap_all_but(url, skip)
    readable = await _readable_with_only(url, NEW_KEK)
    return {
        "left_unrewrapped": skip,
        "rewrapped": rewrapped,
        "readable_after_destroying_the_old_key": readable,
    }


async def run(admin_dsn: str, scratch: str) -> int:
    admin_url = admin_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)

    complete = await _one_run(admin_url, scratch, skip=None)
    partial = await _one_run(admin_url, scratch, skip="gamma")

    print(json.dumps({"complete": complete, "interrupted": partial}, indent=2))

    all_readable = all(complete["readable_after_destroying_the_old_key"].values())
    lost = [
        name
        for name, ok in partial["readable_after_destroying_the_old_key"].items()
        if not ok
    ]

    if not all_readable:
        sys.stderr.write(
            "\nA fully rewrapped secret did not survive the old key going away."
            "\nThat is the rotation being wrong, not the drill.\n"
        )
        return 1
    if lost != ["gamma"]:
        sys.stderr.write(
            f"\nExpected exactly the unrewrapped secret to be lost; lost {lost}.\n"
        )
        return 1

    print(
        "\nRewrapped first, then destroyed: every secret still opens."
        "\nDestroyed with one left behind: that one is gone, permanently, and no"
        "\nlater rotation brings it back — which is what §376's ordering is for."
        "\nThe rewrap endpoint's `remaining`, `unrecoverable` and `unverifiable`"
        "\nmust all read zero before a key is destroyed (docs/operations.md §6)."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="KEK destruction drill")
    parser.add_argument("--admin", required=True, help="DSN able to CREATE DATABASE")
    parser.add_argument("--scratch", default="tiny_hermes_kek_drill")
    parsed = parser.parse_args()
    return asyncio.run(run(str(parsed.admin), str(parsed.scratch)))


if __name__ == "__main__":
    raise SystemExit(main())
