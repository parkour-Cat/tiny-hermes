"""What §5's Run and memory wiring needs that 0030/0031 did not anticipate.

End-user entry design §2, §5. Three unrelated changes share one migration
because they are the three places the plumbing built so far turns out to
still be closed:

**`memories.subject_type`'s CHECK is hand-written**, not generated from
`CallerType` (see the module docstring in
`memory/infrastructure/tables.py`), so widening the enum in an earlier
migration never reached it. An end user cannot become a memory subject until
this constraint says `end_user` is a legal value — otherwise the very first
private memory written for one would be refused by the database, silently,
under a `ProposalOutcome` the model reads as an ordinary refusal rather than
a schema gap.

**`runs.end_user_id` was FK'd to `users.id` only.** That was correct while
the only caller who could ever set it was `caller_type=user` (a platform
member's own id, per §16.3's original stub — see the column's comment in
`runs/infrastructure/tables.py`). Now that a `caller_type=end_user` Run must
set it to a real `end_users.id`, one column would need to satisfy two
different foreign keys depending on a row it cannot see. `sessions.caller_id`
already answers this exact shape of question with no FK at all — a
polymorphic subject reference is checked by `CallerType`, in code, not by the
schema — and this migration brings `runs.end_user_id` in line with that
precedent rather than inventing a second one.

**`end_user_sessions` gains `agents`.** Design's own red line is that a
credential exchanges for a session and is never held onto afterwards — but
§5's two-gate check needs the enterprise's `agents` claim on every Run this
end user starts, which can be long after the 15-minute credential expired.
The session row is therefore where that claim has to live once the
credential that carried it is gone.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0032"
down_revision: str | None = "20260820_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_memories_subject_type", "memories", type_="check")
    op.create_check_constraint(
        "ck_memories_subject_type",
        "memories",
        "subject_type IS NULL OR subject_type IN ('user', 'service_account', 'end_user')",
    )

    op.drop_constraint("fk_runs_end_user", "runs", type_="foreignkey")

    op.add_column(
        "end_user_sessions",
        sa.Column(
            "agents", sa.JSON(), nullable=False, server_default=sa.text("'[]'")
        ),
    )
    op.alter_column("end_user_sessions", "agents", server_default=None)


def downgrade() -> None:
    op.drop_column("end_user_sessions", "agents")

    op.create_foreign_key(
        "fk_runs_end_user", "runs", "users", ["end_user_id"], ["id"], ondelete="RESTRICT"
    )

    op.drop_constraint("ck_memories_subject_type", "memories", type_="check")
    op.create_check_constraint(
        "ck_memories_subject_type",
        "memories",
        "subject_type IS NULL OR subject_type IN ('user', 'service_account')",
    )
