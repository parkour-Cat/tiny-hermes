"""The trace a Run belongs to (observability design §5.3).

Written when the Run is created, as a W3C `traceparent`; every slice and
recovery of the Run is a span under it, whichever process runs it and however
much later. Empty when tracing is off or the creating request was not sampled.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261023_0073"
down_revision: str | None = "20261022_0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("trace_context", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "trace_context")
