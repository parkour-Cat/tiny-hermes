"""Take a backup, lose the data, put it back — and check what the KEK does.

§27.3 item 6's other half; §1134 asks for the backup and restore
instructions this rehearses. The rollback drill next to this one measures
what a downgrade destroys; this one measures whether a restore actually
returns what was there, and what a restored database is worth to somebody
holding the wrong deployment key.

That last part is the reason this exists rather than a paragraph in
`docs/operations.md`. §374 puts the KEK outside the database precisely so a
dump is not a breach, and `docs/operations.md` claims a mismatched KEK
surfaces at *use* rather than at startup. A claim like that is exactly the
kind that is true when written and quietly stops being true, so the drill
checks it instead of repeating it.

Everything happens on scratch databases this script creates. It never reads
or writes the one named by ``DATABASE_URL``, and it never restores over
anything it did not create.

Usage::

    docker run -d --name th-drill-pg -e POSTGRES_USER=tiny_hermes \\
      -e POSTGRES_PASSWORD=local-only -e POSTGRES_DB=postgres \\
      -p 127.0.0.1:55433:5432 postgres:16
    uv run --no-sync python scripts/backup_restore_drill.py \\
      --admin postgresql://tiny_hermes:local-only@127.0.0.1:55433/postgres \\
      --container th-drill-pg
"""

import argparse
import asyncio
import json
import os
import subprocess  # noqa: S404 - pg_dump and alembic are the drill
import sys
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tiny_hermes.secrets.domain.envelope import (
    Envelope,
    UnwrapFailed,
    decode_kek,
    seal,
    unseal,
)

#: The key the secret is sealed under, and the one a restore is mistakenly
#: brought up with. Both are drill-local and neither is a deployment key.
KEK_A = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
KEK_B = "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI="

WATCHED = ("workspaces", "agents", "secrets")


class DrillFailed(RuntimeError):
    pass


@dataclass
class Report:
    before: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    after_loss: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    after_restore: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    alembic_before: str | None = None
    alembic_after: str | None = None
    secret_opens_with_right_kek: bool = False
    secret_opens_with_wrong_kek: bool = True
    wrong_kek_failure: str = ""

    def document(self) -> dict[str, Any]:
        return {
            "rows": {
                table: {
                    "before": self.before.get(table),
                    "after_loss": self.after_loss.get(table),
                    "after_restore": self.after_restore.get(table),
                }
                for table in WATCHED
            },
            "alembic_version": {
                "in_backup": self.alembic_before,
                "after_restore": self.alembic_after,
            },
            "secret": {
                "opens_with_the_deployment_key": self.secret_opens_with_right_kek,
                "opens_with_another_key": self.secret_opens_with_wrong_kek,
                "how_the_wrong_key_fails": self.wrong_kek_failure,
            },
        }


def _run(*argv: str) -> bytes:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, capture_output=True, check=False
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr.decode(errors="replace"))
        raise DrillFailed(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed")
    return result.stdout


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


async def _counts(url: str) -> dict[str, int]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            found: dict[str, int] = {}
            for table in WATCHED:
                value = await connection.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                found[table] = int(value or 0)
            return found
    finally:
        await engine.dispose()


async def _alembic_version(url: str) -> str | None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            return None if value is None else str(value)
    finally:
        await engine.dispose()


async def _seed(url: str) -> None:
    """A workspace, an Agent and one Secret sealed under KEK_A.

    The Secret is the point: rows coming back is the easy half of a restore,
    and whether they are *readable* afterwards is the half that depends on
    something the dump does not contain.
    """
    envelope = seal(b"sk-drill-value", decode_kek(KEK_A), "v1")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            workspace_id = await connection.scalar(
                text(
                    "INSERT INTO workspaces (id, name, status, created_at)"
                    " VALUES (gen_random_uuid(), 'Drill', 'active', now()) RETURNING id"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO agents (id, workspace_id, name, alias, status,"
                    " created_at) VALUES (gen_random_uuid(), :w, 'Drill', 'drill',"
                    " 'draft', now())"
                ),
                {"w": workspace_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO secrets (id, workspace_id, name, scope, status,"
                    " ciphertext, nonce, wrapped_dek, wrap_nonce, key_id, mask,"
                    " created_at, updated_at)"
                    " VALUES (gen_random_uuid(), :w, 'drill', 'workspace', 'active',"
                    " :c, :n, :d, :wn, 'v1', 'sk-***', now(), now())"
                ),
                {
                    "w": workspace_id,
                    "c": envelope.ciphertext,
                    "n": envelope.nonce,
                    "d": envelope.wrapped_dek,
                    "wn": envelope.wrap_nonce,
                },
            )
    finally:
        await engine.dispose()


async def _read_envelope(url: str) -> Envelope:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT ciphertext, nonce, wrapped_dek, wrap_nonce, key_id"
                        " FROM secrets LIMIT 1"
                    )
                )
            ).one()
            return Envelope(
                ciphertext=bytes(row.ciphertext),
                nonce=bytes(row.nonce),
                wrapped_dek=bytes(row.wrapped_dek),
                wrap_nonce=bytes(row.wrap_nonce),
                key_id=str(row.key_id),
            )
    finally:
        await engine.dispose()


async def _rebuild(admin_url: str, name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


async def _lose_everything(url: str) -> None:
    """What a restore has to undo. Not a dropped database — a live one that
    has lost its rows, which is the shape most incidents actually take."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM secrets"))
            await connection.execute(text("DELETE FROM agents"))
            await connection.execute(text("DELETE FROM workspaces"))
    finally:
        await engine.dispose()


async def run(admin_dsn: str, container: str, scratch: str) -> int:
    admin_url = admin_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    scratch_url = f"{admin_url.rsplit('/', 1)[0]}/{scratch}"
    report = Report()

    await _rebuild(admin_url, scratch)
    _alembic(scratch_url, "upgrade", "head")
    await _seed(scratch_url)

    report.before = await _counts(scratch_url)
    report.alembic_before = await _alembic_version(scratch_url)

    # Plain file I/O rather than pathlib: everything in this function blocks
    # by design — it is a sequence of steps with nothing else on the loop —
    # and pathlib is the only part of that a linter can see.
    dump_path = f"/tmp/{scratch}.dump"  # noqa: S108 - drill artifact, named per run
    with open(dump_path, "wb") as handle:  # noqa: ASYNC230 - see above
        handle.write(
            _run("docker", "exec", container, "pg_dump", "-U", "tiny_hermes", "-Fc", scratch)
        )

    await _lose_everything(scratch_url)
    report.after_loss = await _counts(scratch_url)

    # `docker cp` rather than piping: pg_restore needs a seekable file for a
    # custom-format dump, and a pipe is not one.
    inside = f"/tmp/{scratch}.dump"  # noqa: S108 - path inside the drill's own container
    _run("docker", "cp", dump_path, f"{container}:{inside}")
    # The flags `docs/operations.md` tells an operator to use, so this drill
    # rehearses the runbook rather than a variant of it. `--data-only` was
    # tried first and is wrong: it restores tables in alphabetical order with
    # constraints live, so `agents` lands before `workspaces` and its foreign
    # key fails, and `alembic_version` collides with the row already there.
    # `--clean` drops and recreates, which is what makes the order a
    # non-question.
    _run(
        "docker", "exec", container, "pg_restore", "-U", "tiny_hermes",
        "-d", scratch, "--clean", "--if-exists", inside,
    )

    report.after_restore = await _counts(scratch_url)
    report.alembic_after = await _alembic_version(scratch_url)

    envelope = await _read_envelope(scratch_url)
    try:
        opened = unseal(envelope, decode_kek(KEK_A))
        report.secret_opens_with_right_kek = opened == b"sk-drill-value"
    except UnwrapFailed:
        report.secret_opens_with_right_kek = False
    try:
        unseal(envelope, decode_kek(KEK_B))
        report.secret_opens_with_wrong_kek = True
    except UnwrapFailed as error:
        report.secret_opens_with_wrong_kek = False
        report.wrong_kek_failure = type(error).__name__

    print(json.dumps(report.document(), indent=2, ensure_ascii=False))

    restored = report.after_restore == report.before
    print(
        "\nRestore returned every watched row."
        if restored
        else "\nRestore did NOT return what was backed up — read the table above."
    )
    if not report.secret_opens_with_wrong_kek:
        print(
            "A dump restored under the wrong KEK yields unreadable secrets"
            f" ({report.wrong_kek_failure}) — §374 holding. Note where it fails:"
            "\nat the moment a secret is used, not at startup, so a deployment"
            "\nbrought up with the wrong key looks healthy until something needs one."
        )
    os.unlink(dump_path)
    return 0 if restored and not report.secret_opens_with_wrong_kek else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup and restore drill")
    parser.add_argument("--admin", required=True, help="DSN able to CREATE DATABASE")
    parser.add_argument("--container", required=True, help="Postgres container name")
    parser.add_argument("--scratch", default="tiny_hermes_backup_drill")
    parsed = parser.parse_args()
    return asyncio.run(run(parsed.admin, parsed.container, parsed.scratch))


if __name__ == "__main__":
    raise SystemExit(main())
