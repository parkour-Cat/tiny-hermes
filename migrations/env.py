import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from tiny_hermes.agents.infrastructure import tables as agent_tables  # noqa: F401
from tiny_hermes.artifacts.infrastructure import tables as artifact_tables  # noqa: F401
from tiny_hermes.audit.infrastructure import tables as audit_tables  # noqa: F401
from tiny_hermes.channels.infrastructure import tables as channel_tables  # noqa: F401
from tiny_hermes.http_tools.infrastructure import tables as http_tool_tables  # noqa: F401
from tiny_hermes.identity.infrastructure import (  # noqa: F401
    end_user_session_tables as end_user_session_tables,
)
from tiny_hermes.identity.infrastructure import end_user_tables as end_user_tables  # noqa: F401
from tiny_hermes.identity.infrastructure import tables as identity_tables  # noqa: F401
from tiny_hermes.mcp.infrastructure import tables as mcp_tables  # noqa: F401
from tiny_hermes.memory.infrastructure import tables as memory_tables  # noqa: F401
from tiny_hermes.model_catalog.infrastructure import (  # noqa: F401
    pricing_tables as pricing_tables,
)
from tiny_hermes.model_catalog.infrastructure import tables as endpoint_tables  # noqa: F401
from tiny_hermes.outbound.infrastructure import tables as outbound_tables  # noqa: F401
from tiny_hermes.runs.infrastructure import tables as run_tables  # noqa: F401
from tiny_hermes.sandbox.infrastructure import tables as sandbox_tables  # noqa: F401
from tiny_hermes.secrets.infrastructure import tables as secret_tables  # noqa: F401
from tiny_hermes.session_workspace.infrastructure import (  # noqa: F401
    tables as workspace_tables,
)
from tiny_hermes.shared.database import Base
from tiny_hermes.skills.infrastructure import tables as skill_tables  # noqa: F401
from tiny_hermes.tenancy.infrastructure import tables as tenancy_tables  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"].replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported")
asyncio.run(run_online())
