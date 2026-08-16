from uuid import uuid4

import pytest
from tiny_hermes.model_catalog.infrastructure.credentials import (
    CredentialMissing,
    CredentialResolver,
    secret_id_from_ref,
)
from tiny_hermes.secrets.application.service import KekSettings, SecretService
from tiny_hermes.secrets.domain.envelope import decode_kek
from tiny_hermes.secrets.domain.models import SecretScope
from tiny_hermes.secrets.infrastructure.memory_store import MemorySecretStore
from tiny_hermes.tenancy.domain.models import Actor, Role

TEST_KEK = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
OTHER_KEK = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="


def test_an_env_name_is_not_a_secret_id() -> None:
    assert secret_id_from_ref("TINY_HERMES_MODEL_KEY") is None


def test_a_uuid_is_a_secret_id() -> None:
    secret_id = uuid4()
    assert secret_id_from_ref(str(secret_id)) == secret_id


@pytest.mark.asyncio
async def test_resolve_unwraps_an_active_secret() -> None:
    store = MemorySecretStore()
    workspace_id = uuid4()
    admin = Actor(uuid4(), False)
    store.memberships[(workspace_id, admin.id)] = Role.WORKSPACE_ADMIN
    created = await SecretService(store, KekSettings(TEST_KEK, "v1")).create(
        admin, workspace_id, "openai", SecretScope.WORKSPACE, "sk-from-secret", "req"
    )
    resolver = CredentialResolver(store, decode_kek(TEST_KEK))

    assert await resolver.is_available(str(created.id)) is True
    assert await resolver.resolve(str(created.id)) == "sk-from-secret"


@pytest.mark.asyncio
async def test_the_wrong_kek_does_not_change_the_row() -> None:
    store = MemorySecretStore()
    workspace_id = uuid4()
    admin = Actor(uuid4(), False)
    store.memberships[(workspace_id, admin.id)] = Role.WORKSPACE_ADMIN
    created = await SecretService(store, KekSettings(TEST_KEK, "v1")).create(
        admin, workspace_id, "openai", SecretScope.WORKSPACE, "sk-from-secret", "req"
    )
    before = store.records[created.id]
    resolver = CredentialResolver(store, decode_kek(OTHER_KEK))

    with pytest.raises(CredentialMissing):
        await resolver.resolve(str(created.id))

    after = store.records[created.id]
    assert after.ciphertext == before.ciphertext
    assert after.wrapped_dek == before.wrapped_dek
    assert after.key_id == before.key_id


@pytest.mark.asyncio
async def test_a_disabled_secret_is_not_available() -> None:
    store = MemorySecretStore()
    workspace_id = uuid4()
    admin = Actor(uuid4(), False)
    store.memberships[(workspace_id, admin.id)] = Role.WORKSPACE_ADMIN
    service = SecretService(store, KekSettings(TEST_KEK, "v1"))
    created = await service.create(
        admin, workspace_id, "openai", SecretScope.WORKSPACE, "sk-from-secret", "req"
    )
    await service.disable(admin, workspace_id, created.id, "req-2")
    resolver = CredentialResolver(store, decode_kek(TEST_KEK))

    assert await resolver.is_available(str(created.id)) is False
    with pytest.raises(CredentialMissing):
        await resolver.resolve(str(created.id))
