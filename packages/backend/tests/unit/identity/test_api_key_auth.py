from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tiny_hermes.identity.application.machine_service import (
    InvalidApiKey,
    MachineIdentityService,
    WorkspaceBindingMismatch,
)
from tiny_hermes.identity.domain.models import TOKEN_PREFIX, ServiceAccountStatus
from tiny_hermes.identity.infrastructure.memory_machine_store import MemoryMachineIdentityStore
from tiny_hermes.tenancy.domain.models import Actor, Role

ADMIN = Actor(uuid4(), False)


async def _issued(
    store: MemoryMachineIdentityStore,
    *,
    role: Role = Role.DEVELOPER,
    scopes: tuple[str, ...] = ("runs.read", "runs.write"),
    expires_at: datetime | None = None,
):
    workspace_id = uuid4()
    store.memberships[(workspace_id, ADMIN.id)] = Role.WORKSPACE_ADMIN
    service = MachineIdentityService(store)
    account = await service.create_account(
        ADMIN, workspace_id, "ci-bot", role, "req-account"
    )
    issued = await service.create_key(
        ADMIN, workspace_id, account.id, scopes, "req-key", expires_at=expires_at
    )
    return service, account, issued


@pytest.mark.asyncio
async def test_a_matching_digest_names_the_account_not_the_key() -> None:
    store = MemoryMachineIdentityStore()
    service, account, issued = await _issued(store)

    machine = await service.authenticate(issued.token, None)

    assert machine.account.id == account.id
    assert machine.key.id == issued.key.id
    assert machine.scopes == frozenset({"runs.read", "runs.write"})
    assert issued.token.startswith(TOKEN_PREFIX)
    assert issued.token not in {digest for _, digest in store.keys.values()}


@pytest.mark.asyncio
async def test_a_wrong_token_is_rejected_without_naming_the_reason() -> None:
    store = MemoryMachineIdentityStore()
    service, _, issued = await _issued(store)
    with pytest.raises(InvalidApiKey):
        await service.authenticate(issued.token[:-1] + "0", None)


@pytest.mark.asyncio
async def test_a_revoked_key_is_rejected() -> None:
    store = MemoryMachineIdentityStore()
    service, account, issued = await _issued(store)
    await service.revoke_key(ADMIN, account.workspace_id, issued.key.id, "req-revoke")
    with pytest.raises(InvalidApiKey):
        await service.authenticate(issued.token, None)


@pytest.mark.asyncio
async def test_an_expired_key_is_rejected() -> None:
    store = MemoryMachineIdentityStore()
    service, _, issued = await _issued(
        store, expires_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    with pytest.raises(InvalidApiKey):
        await service.authenticate(issued.token, None)


@pytest.mark.asyncio
async def test_a_disabled_account_is_rejected() -> None:
    store = MemoryMachineIdentityStore()
    service, account, issued = await _issued(store)
    await service.disable_account(ADMIN, account.workspace_id, account.id, "req-disable")
    assert store.accounts[account.id].status is ServiceAccountStatus.DISABLED
    with pytest.raises(InvalidApiKey):
        await service.authenticate(issued.token, None)


@pytest.mark.asyncio
async def test_a_workspace_header_that_disagrees_is_a_generic_mismatch() -> None:
    store = MemoryMachineIdentityStore()
    service, _, issued = await _issued(store)
    with pytest.raises(WorkspaceBindingMismatch):
        await service.authenticate(issued.token, uuid4())
