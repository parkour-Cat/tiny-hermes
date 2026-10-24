"""A sandbox instance is registered before its container is built (§21.3, v2.24).

The admission lock now covers counting and registering only; the container is
created after it is released. Until then the row is `creating` and has no
container id.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261024_0080"
down_revision: str | None = "20261023_0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WITH = "status IN ('creating', 'running', 'frozen', 'isolated', 'destroyed')"
_WITHOUT = "status IN ('running', 'frozen', 'isolated', 'destroyed')"


def upgrade() -> None:
    op.alter_column("sandbox_instances", "container_id", nullable=True)
    op.drop_constraint("ck_sandbox_instances_status", "sandbox_instances", type_="check")
    op.create_check_constraint("ck_sandbox_instances_status", "sandbox_instances", _WITH)


def downgrade() -> None:
    # A row still `creating` has no container id and no state to go back to.
    creating = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM sandbox_instances WHERE status = 'creating'")
    )
    if creating:
        raise RuntimeError("sandbox instances are still being created; let them finish first")
    op.drop_constraint("ck_sandbox_instances_status", "sandbox_instances", type_="check")
    op.create_check_constraint("ck_sandbox_instances_status", "sandbox_instances", _WITHOUT)
    op.alter_column("sandbox_instances", "container_id", nullable=False)
