from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tiny_hermes.secrets.domain.models import SecretRecord, SecretScope, SecretStatus
from tiny_hermes.secrets.infrastructure.sql_store import SqlSecretStore
from tiny_hermes.secrets.ports.store import DuplicateSecretName
from tiny_hermes.tenancy.infrastructure.tables import WorkspaceRow

pytestmark = pytest.mark.usefixtures("empty_database")

Sessions = async_sessionmaker[AsyncSession]


async def _workspace(engine: AsyncEngine) -> tuple[UUID, Sessions]:
    factory: Sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        row = WorkspaceRow(name="Acme", status="active")
        session.add(row)
        await session.commit()
        return row.id, factory


def _record(
    *, name: str, scope: SecretScope, workspace_id: UUID | None = None
) -> SecretRecord:
    now = datetime.now(UTC)
    return SecretRecord(
        id=uuid4(),
        name=name,
        scope=scope,
        workspace_id=workspace_id,
        status=SecretStatus.ACTIVE,
        mask="ab••••yz",
        ciphertext=b"cipher",
        nonce=b"n" * 12,
        wrapped_dek=b"wrapped",
        wrap_nonce=b"w" * 12,
        key_id="v1",
        created_at=now,
        updated_at=now,
    )


async def test_sql_store_refuses_a_duplicate_workspace_name(engine: AsyncEngine) -> None:
    workspace_id, factory = await _workspace(engine)
    async with factory() as session:
        await SqlSecretStore(session).create(
            _record(name="openai", scope=SecretScope.WORKSPACE, workspace_id=workspace_id)
        )
        await session.commit()
    async with factory() as session:
        with pytest.raises(DuplicateSecretName):
            await SqlSecretStore(session).create(
                _record(
                    name="openai", scope=SecretScope.WORKSPACE, workspace_id=workspace_id
                )
            )


async def test_sql_store_refuses_a_duplicate_platform_name(engine: AsyncEngine) -> None:
    _, factory = await _workspace(engine)
    async with factory() as session:
        await SqlSecretStore(session).create(
            _record(name="openai", scope=SecretScope.PLATFORM, workspace_id=None)
        )
        await session.commit()
    async with factory() as session:
        with pytest.raises(DuplicateSecretName):
            await SqlSecretStore(session).create(
                _record(name="openai", scope=SecretScope.PLATFORM, workspace_id=None)
            )
