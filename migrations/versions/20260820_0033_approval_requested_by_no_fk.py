"""`approvals.requested_by` stops being FK'd to `users.id` alone.

End-user entry design §2, §5. `USER_CONFIRMATION` got its first producer in
0032 (`runs/infrastructure/sql_approvals.py::SqlApprovalGate._subject` returns
`run.end_user_id` for that `approval_type`), but nothing had exercised that
path against a real database until this task tried to build the consumer —
the end user's own approval endpoint. It surfaced immediately: `run.end_user_
id` is sometimes an `end_users.id` now, and a FK written when the only caller
was a workspace member's own id has no way to accept that.

Same shape 0032 already solved for `runs.end_user_id`, for the same reason:
one column cannot satisfy two foreign keys chosen by a row it cannot see.
`sessions.caller_id` is still the precedent — a polymorphic subject reference
is a `CallerType`/`ApprovalType` check in code, not a constraint the schema
can express — and this migration brings `approvals.requested_by` in line
with it rather than inventing a second shape for the same problem.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260820_0033"
down_revision: str | None = "20260820_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("fk_approvals_requested_by", "approvals", type_="foreignkey")


def downgrade() -> None:
    op.create_foreign_key(
        "fk_approvals_requested_by",
        "approvals",
        "users",
        ["requested_by"],
        ["id"],
        ondelete="RESTRICT",
    )
