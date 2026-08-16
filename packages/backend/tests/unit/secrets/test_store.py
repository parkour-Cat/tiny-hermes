from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tiny_hermes.secrets.domain.models import SecretRecord, SecretScope, SecretStatus
from tiny_hermes.secrets.infrastructure.memory_store import MemorySecretStore
from tiny_hermes.secrets.ports.store import DuplicateSecretName


def _record(
    *,
    name: str = "openai",
    scope: SecretScope = SecretScope.WORKSPACE,
    workspace_id: UUID | None = None,
) -> SecretRecord:
    now = datetime.now(UTC)
    chosen_workspace = workspace_id
    if scope is SecretScope.WORKSPACE and chosen_workspace is None:
        chosen_workspace = uuid4()
    return SecretRecord(
        id=uuid4(),
        name=name,
        scope=scope,
        workspace_id=chosen_workspace,
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


@pytest.mark.asyncio
async def test_workspace_names_are_unique_inside_one_workspace() -> None:
    store = MemorySecretStore()
    first = _record(name="openai")
    await store.create(first)
    with pytest.raises(DuplicateSecretName):
        await store.create(_record(name="openai", workspace_id=first.workspace_id))


@pytest.mark.asyncio
async def test_the_same_name_may_exist_in_two_workspaces() -> None:
    store = MemorySecretStore()
    await store.create(_record(name="openai"))
    second = await store.create(_record(name="openai"))
    assert second.name == "openai"


@pytest.mark.asyncio
async def test_platform_names_are_unique() -> None:
    store = MemorySecretStore()
    await store.create(_record(name="openai", scope=SecretScope.PLATFORM, workspace_id=None))
    with pytest.raises(DuplicateSecretName):
        await store.create(
            _record(name="openai", scope=SecretScope.PLATFORM, workspace_id=None)
        )


@pytest.mark.asyncio
async def test_a_workspace_and_a_platform_secret_may_share_a_name() -> None:
    store = MemorySecretStore()
    workspace = await store.create(_record(name="openai"))
    platform = await store.create(
        _record(name="openai", scope=SecretScope.PLATFORM, workspace_id=None)
    )
    assert workspace.workspace_id is not None
    assert platform.workspace_id is None


@pytest.mark.asyncio
async def test_listing_a_workspace_includes_platform_secrets() -> None:
    store = MemorySecretStore()
    workspace = await store.create(_record(name="workspace-key"))
    await store.create(_record(name="other", workspace_id=uuid4()))
    platform = await store.create(
        _record(name="platform-key", scope=SecretScope.PLATFORM, workspace_id=None)
    )
    assert workspace.workspace_id is not None
    visible = await store.list_visible(workspace.workspace_id)
    assert {item.id for item in visible} == {workspace.id, platform.id}


@pytest.mark.asyncio
async def test_a_stored_record_has_no_plaintext_attribute() -> None:
    record = _record()
    assert not hasattr(record, "plaintext")
    assert record.mask != ""
