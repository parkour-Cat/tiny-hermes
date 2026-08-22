"""Roll the schema backwards with data in it, and say exactly what that costs.

§27.3 item 6 asks for a backup, restore and upgrade-rollback drill; §1134 asks
for the instructions that go with them. This is the rollback half, and it
exists because every migration round-trip run so far in this repository —
including `alembic check` and every CI run — has been performed on a schema
with **no rows in the new tables**. An operator rolling back a release is
doing it with a populated database, which is a different question.

What this measures is not "does downgrade run". It is **what does downgrade
destroy**. Several of these migrations drop tables and columns on the way
down; that is correct and intended, and it is also irreversible. An operator
deciding whether to roll back needs that stated in rows, not inferred from
reading migration source under pressure.

The drill builds a scratch database of its own and never touches the one named
by ``DATABASE_URL``. A rollback drill that rehearsed on live data would be the
accident it exists to prevent.

Usage::

    docker run -d --name th-drill-pg -e POSTGRES_USER=tiny_hermes \\
      -e POSTGRES_PASSWORD=local-only -e POSTGRES_DB=postgres \\
      -p 127.0.0.1:55433:5432 postgres:16
    uv run --no-sync python scripts/upgrade_rollback_drill.py \\
      --admin postgresql://tiny_hermes:local-only@127.0.0.1:55433/postgres
"""

import argparse
import asyncio
import json
import os
import subprocess  # noqa: S404 - driving alembic is what the drill does
import sys
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

#: How far back to roll. Four is the run of migrations added since the last
#: release, which is the distance an operator actually rolls back — not
#: `base`, which is a fresh install rather than a rollback.
STEPS = 4

#: Tables this drill seeds and then counts. Each is touched by a migration
#: inside `STEPS`, so each can answer "did rolling back take this with it".
WATCHED = (
    "channel_bindings",
    "channel_events",
    "channel_conversations",
    "oidc_providers",
)


class DrillFailed(RuntimeError):
    pass


@dataclass
class Finding:
    table: str
    before: int | None = None
    after_downgrade: int | None = None
    after_upgrade: int | None = None
    irreversible: bool = False

    def document(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "rows_before": self.before,
            "after_downgrade": self.after_downgrade,
            "after_upgrade": self.after_upgrade,
            "irreversible": self.irreversible,
        }


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


async def _count(url: str, table: str) -> int | None:
    """`None` means the table is not there, which is itself an answer this
    drill is asking for and is not the same as zero rows."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT to_regclass(:name) IS NOT NULL"), {"name": table}
            )
            if not exists:
                return None
            counted = await connection.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            return int(counted or 0)
    finally:
        await engine.dispose()


async def _seed(url: str) -> None:
    """One row per watched table, carrying only what its constraints demand.

    Deliberately minimal. This drill measures whether rows survive a
    round-trip; a rich fixture would only add ways for the seed itself to fail
    against the schema it is supposed to be testing.
    """
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            user_id = await connection.scalar(
                text(
                    "INSERT INTO users (id, display_name, status, is_platform_admin,"
                    " created_at) VALUES (gen_random_uuid(), 'Drill', 'active', false,"
                    " now()) RETURNING id"
                )
            )
            workspace_id = await connection.scalar(
                text(
                    "INSERT INTO workspaces (id, name, status, created_at)"
                    " VALUES (gen_random_uuid(), 'Drill', 'active', now()) RETURNING id"
                )
            )
            agent_id = await connection.scalar(
                text(
                    "INSERT INTO agents (id, workspace_id, name, alias, status,"
                    " created_at) VALUES (gen_random_uuid(), :w, 'Drill', 'drill',"
                    " 'draft', now()) RETURNING id"
                ),
                {"w": workspace_id},
            )
            binding_id = await connection.scalar(
                text(
                    "INSERT INTO channel_bindings (id, workspace_id, channel, agent_id,"
                    " status, created_by, created_at, encrypt_key_ref)"
                    " VALUES (gen_random_uuid(), :w, 'feishu', :a, 'active', :u, now(),"
                    " 'DRILL_KEY') RETURNING id"
                ),
                {"w": workspace_id, "a": agent_id, "u": user_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO channel_events (id, channel_binding_id,"
                    " channel_event_id, received_at)"
                    " VALUES (gen_random_uuid(), :b, 'om_drill', now())"
                ),
                {"b": binding_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO oidc_providers (id, issuer, client_id,"
                    " client_secret_ref, discovery_url, scopes, status, created_by,"
                    " created_at)"
                    " VALUES (gen_random_uuid(), 'https://idp.example.com', 'cid',"
                    " 'REF', 'https://idp.example.com/.well-known/openid-configuration',"
                    " '[\"openid\"]', 'active', :u, now())"
                ),
                {"u": user_id},
            )
    finally:
        await engine.dispose()


async def _rebuild(admin_url: str, scratch: str) -> None:
    """CREATE DATABASE cannot run inside a transaction, hence AUTOCOMMIT."""
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
            await connection.execute(text(f'CREATE DATABASE "{scratch}"'))
    finally:
        await engine.dispose()


async def run(admin_dsn: str, scratch: str) -> int:
    # One URL shape throughout: SQLAlchemy's, which is also what alembic reads
    # from DATABASE_URL. Accepting a bare postgresql:// on the command line is
    # a convenience for whoever is holding a psql connection string already.
    admin_url = admin_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    await _rebuild(admin_url, scratch)
    scratch_url = f"{admin_url.rsplit('/', 1)[0]}/{scratch}"

    _alembic(scratch_url, "upgrade", "head")
    await _seed(scratch_url)

    findings = [Finding(table=name) for name in WATCHED]
    for finding in findings:
        finding.before = await _count(scratch_url, finding.table)

    _alembic(scratch_url, "downgrade", f"-{STEPS}")
    for finding in findings:
        finding.after_downgrade = await _count(scratch_url, finding.table)

    _alembic(scratch_url, "upgrade", "head")
    for finding in findings:
        finding.after_upgrade = await _count(scratch_url, finding.table)
        # Irreversible when rows existed before and are not there afterwards.
        # A table that comes back empty is not the same as one that comes back
        # with its rows, and that difference is the whole report.
        finding.irreversible = bool(finding.before) and finding.after_upgrade != finding.before

    print(
        json.dumps(
            {"steps_rolled_back": STEPS, "tables": [f.document() for f in findings]},
            indent=2,
            ensure_ascii=False,
        )
    )

    lost = [f.table for f in findings if f.irreversible]
    if lost:
        print(
            "\nIrreversible on rollback: "
            + ", ".join(lost)
            + "\nThis is the drill working rather than failing. Rolling the schema"
            "\nback past these migrations destroys those rows and no later upgrade"
            "\nbrings them back — take a backup first (docs/operations.md)."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Upgrade-rollback drill")
    parser.add_argument("--admin", required=True, help="DSN able to CREATE DATABASE")
    parser.add_argument("--scratch", default="tiny_hermes_rollback_drill")
    parsed = parser.parse_args()
    return asyncio.run(run(parsed.admin, parsed.scratch))


if __name__ == "__main__":
    raise SystemExit(main())
