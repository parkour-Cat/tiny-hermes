"""Row-level security on every table with a `workspace_id` (§9.3, v2.20).

The application acts as `tiny_hermes_app`: not a superuser, not the tables'
owner, no BYPASSRLS. Row-level security ignores all three, and the login
Compose ships is all three, so connections step down to this role when they
open (`DATABASE_ROLE`). It is created without a login of its own; a deployment
that wants one gives it a password and logs in as it directly.

A transaction carrying `app.workspace_id` sees and writes that workspace's
rows only, and the platform's rows where `workspace_id` may be null. A
transaction without it is not restricted: the Worker, the Scheduler and the
rest work across workspaces by design.

The role is not dropped on downgrade: roles belong to the cluster, and the
test database on the same cluster may still be using it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261023_0075"
down_revision: str | None = "20261023_0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "tiny_hermes_app"

#: Every table with a `workspace_id` at this revision. A table added later
#: needs its own migration to join; `test_row_level_security` fails until it
#: does.
TENANT_TABLES = (
    "agent_versions",
    "agents",
    "approvals",
    "artifact_grants",
    "artifacts",
    "channel_bindings",
    "channel_issuers",
    "end_user_sessions",
    "end_users",
    "external_identities",
    "http_tools",
    "idempotency_records",
    "mcp_servers",
    "memberships",
    "memories",
    "object_uploads",
    "run_events",
    "run_steers",
    "runs",
    "sandbox_reservations",
    "service_accounts",
    "session_compactions",
    "session_messages",
    "sessions",
    "skill_version_loads",
    "workspace_revisions",
)
#: Where a null `workspace_id` is the platform's own row, which belongs to no
#: other tenant and stays visible inside every workspace.
PLATFORM_ROWS_TOO = ("audit_events", "outbound_scopes", "secrets", "skill_proposals", "skills")

_SCOPED = (
    "coalesce(current_setting('app.workspace_id', true), '') = '' "
    "OR workspace_id = current_setting('app.workspace_id', true)::uuid"
)


def upgrade() -> None:
    op.execute(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
            END IF;
        END $$
        """
    )
    # So the login that owns the tables may `SET ROLE` to it without being a
    # superuser.
    op.execute(f"GRANT {ROLE} TO CURRENT_USER")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ROLE}")
    op.execute(f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {ROLE}")
    # Tables later migrations create, by the same login, are granted as well.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {ROLE}"
    )
    for table in TENANT_TABLES:
        _protect(table, _SCOPED)
    for table in PLATFORM_ROWS_TOO:
        _protect(table, f"workspace_id IS NULL OR {_SCOPED}")


def _protect(table: str, condition: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_isolation ON {table} "
        f"USING ({condition}) WITH CHECK ({condition})"
    )


def downgrade() -> None:
    for table in (*TENANT_TABLES, *PLATFORM_ROWS_TOO):
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM {ROLE}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {ROLE}"
    )
    op.execute(f"REVOKE USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}")
