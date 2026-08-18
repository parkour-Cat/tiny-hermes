"""Every foreign key resolves in a process that imports only this module.

SQLAlchemy resolves a foreign key by table *name*, at flush time, against
whatever is registered on the shared metadata. The API never notices a missing
registration because its routers import every table module anyway; a Worker
imports far less, and the first Agent that proposed a skill change met
`NoReferencedTableError: could not find table 'users'` in the middle of
answering a tool call.

So this runs in a subprocess. In-process it would pass no matter what, because
the rest of the suite has already imported everything — which is exactly the
blind spot that let the bug ship.
"""

import subprocess
import sys

#: Imports nothing but the skills tables, then asks SQLAlchemy to resolve every
#: foreign key on them. `fk.column` is the lazy resolution that raises.
PROBE = """
from sqlalchemy import inspect

from tiny_hermes.skills.infrastructure.tables import (
    SkillFileRow,
    SkillProposalRow,
    SkillRow,
    SkillVersionRow,
)

names = set()
for row in (SkillRow, SkillVersionRow, SkillFileRow, SkillProposalRow):
    for key in inspect(row).local_table.foreign_keys:
        names.add(key.column.table.name)
print(",".join(sorted(names)))
"""


def test_the_skill_tables_resolve_their_keys_without_the_rest_of_the_app() -> None:
    finished = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        check=False,
    )

    assert finished.returncode == 0, finished.stderr
    assert finished.stdout.strip().split(",") == [
        "runs",
        "skill_versions",
        "skills",
        "users",
        "workspaces",
    ]
