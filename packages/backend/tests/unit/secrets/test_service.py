from uuid import UUID, uuid4

import pytest
from tiny_hermes.secrets.application.service import (
    ForbiddenSecretAction,
    KekMissing,
    KekSettings,
    PreviousKekMissing,
    SecretNameTaken,
    SecretService,
    UnknownSecret,
)
from tiny_hermes.secrets.domain.envelope import decode_kek, unseal
from tiny_hermes.secrets.domain.models import SecretRecord, SecretScope, SecretStatus
from tiny_hermes.secrets.infrastructure.memory_store import MemorySecretStore
from tiny_hermes.tenancy.domain.models import Actor, Role

TEST_KEK = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
KEK = KekSettings(current=TEST_KEK, current_id="v1")
ADMIN = Actor(uuid4(), False)
PLATFORM = Actor(uuid4(), True)


def _service(
    store: MemorySecretStore | None = None, *, kek: KekSettings = KEK
) -> tuple[MemorySecretStore, SecretService, UUID]:
    memory = store or MemorySecretStore()
    workspace_id = uuid4()
    memory.memberships[(workspace_id, ADMIN.id)] = Role.WORKSPACE_ADMIN
    return memory, SecretService(memory, kek), workspace_id


@pytest.mark.asyncio
async def test_create_returns_a_mask_and_stores_ciphertext() -> None:
    store, service, workspace_id = _service()

    created = await service.create(
        ADMIN, workspace_id, "openai", SecretScope.WORKSPACE, "sk-secret-value", "req-1"
    )

    assert created.mask != "sk-secret-value"
    assert created.mask.startswith("sk")
    assert created.mask.endswith("ue")
    assert created.status is SecretStatus.ACTIVE
    stored = store.records[created.id]
    assert unseal(stored.envelope(), decode_kek(TEST_KEK)) == b"sk-secret-value"
    assert "plaintext" not in created.__dict__
    assert "secret.created" in store.audit_actions


@pytest.mark.asyncio
async def test_listing_never_includes_plaintext_or_ciphertext() -> None:
    _, service, workspace_id = _service()
    await service.create(
        ADMIN, workspace_id, "openai", SecretScope.WORKSPACE, "sk-secret-value", "req-1"
    )

    listed = await service.list(ADMIN, workspace_id, "req-2")

    assert len(listed) == 1
    assert listed[0].mask != "sk-secret-value"
    assert not hasattr(listed[0], "plaintext")
    assert not hasattr(listed[0], "ciphertext")


@pytest.mark.asyncio
async def test_a_duplicate_name_is_refused() -> None:
    _, service, workspace_id = _service()
    await service.create(
        ADMIN, workspace_id, "openai", SecretScope.WORKSPACE, "one", "req-1"
    )
    with pytest.raises(SecretNameTaken):
        await service.create(
            ADMIN, workspace_id, "openai", SecretScope.WORKSPACE, "two", "req-2"
        )


@pytest.mark.asyncio
async def test_a_viewer_cannot_list_or_write() -> None:
    store, service, workspace_id = _service()
    store.memberships[(workspace_id, ADMIN.id)] = Role.VIEWER
    with pytest.raises(ForbiddenSecretAction):
        await service.list(ADMIN, workspace_id, "req-list")
    with pytest.raises(ForbiddenSecretAction):
        await service.create(
            ADMIN, workspace_id, "openai", SecretScope.WORKSPACE, "value", "req-write"
        )


@pytest.mark.asyncio
async def test_a_developer_can_list_names_but_not_write() -> None:
    store, _, workspace_id = _service()
    await SecretService(store, KEK).create(
        ADMIN, workspace_id, "openai", SecretScope.WORKSPACE, "value", "req-1"
    )
    developer = Actor(uuid4(), False)
    store.memberships[(workspace_id, developer.id)] = Role.DEVELOPER
    service = SecretService(store, KEK)

    listed = await service.list(developer, workspace_id, "req-list")
    assert [item.name for item in listed] == ["openai"]
    with pytest.raises(ForbiddenSecretAction):
        await service.create(
            developer, workspace_id, "other", SecretScope.WORKSPACE, "value", "req-write"
        )


@pytest.mark.asyncio
async def test_a_workspace_admin_cannot_write_a_platform_secret() -> None:
    _, service, workspace_id = _service()
    with pytest.raises(ForbiddenSecretAction):
        await service.create(
            ADMIN, workspace_id, "openai", SecretScope.PLATFORM, "value", "req-1"
        )


@pytest.mark.asyncio
async def test_a_platform_admin_can_write_a_platform_secret() -> None:
    store, service, workspace_id = _service()

    created = await service.create(
        PLATFORM, workspace_id, "openai", SecretScope.PLATFORM, "value", "req-1"
    )

    assert created.scope is SecretScope.PLATFORM
    assert created.workspace_id is None
    assert "secret.platform_write" in store.audit_actions


@pytest.mark.asyncio
async def test_disable_leaves_ciphertext_in_place() -> None:
    store, service, workspace_id = _service()
    created = await service.create(
        ADMIN, workspace_id, "openai", SecretScope.WORKSPACE, "sk-secret-value", "req-1"
    )
    before = store.records[created.id]

    disabled = await service.disable(ADMIN, workspace_id, created.id, "req-2")

    assert disabled.status is SecretStatus.DISABLED
    after = store.records[created.id]
    assert after.ciphertext == before.ciphertext
    assert after.wrapped_dek == before.wrapped_dek


@pytest.mark.asyncio
async def test_a_secret_from_another_workspace_is_unknown() -> None:
    _, service, workspace_id = _service()
    created = await service.create(
        ADMIN, workspace_id, "openai", SecretScope.WORKSPACE, "value", "req-1"
    )
    with pytest.raises(UnknownSecret):
        await service.disable(ADMIN, uuid4(), created.id, "req-2")


@pytest.mark.asyncio
async def test_a_missing_kek_refuses_the_write() -> None:
    _, service, workspace_id = _service(kek=KekSettings(current="", current_id="v1"))
    with pytest.raises(KekMissing):
        await service.create(
            ADMIN, workspace_id, "openai", SecretScope.WORKSPACE, "value", "req-1"
        )


@pytest.mark.asyncio
async def test_a_service_account_cannot_manage_secrets() -> None:
    store, service, workspace_id = _service()
    machine = Actor(uuid4(), False, is_service_account=True, role=Role.DEVELOPER)
    store.memberships[(workspace_id, machine.id)] = Role.DEVELOPER
    with pytest.raises(ForbiddenSecretAction):
        await service.list(machine, workspace_id, "req-list")
    with pytest.raises(ForbiddenSecretAction):
        await service.create(
            machine, workspace_id, "openai", SecretScope.WORKSPACE, "value", "req-write"
        )


OTHER_KEK = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="
#: A third key nobody hands the rotation, so a record sealed under it is
#: unopenable by the previous KEK — a lost key, or one rotated out of order.
THIRD_KEK = "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI="


class InterruptAfterOne(MemorySecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.writes = 0
        self.interrupted = False

    async def replace_wrap(
        self,
        secret_id: UUID,
        wrapped_dek: bytes,
        wrap_nonce: bytes,
        key_id: str,
    ) -> SecretRecord | None:
        if self.writes >= 1 and not self.interrupted:
            self.interrupted = True
            raise RuntimeError("interrupted")
        self.writes += 1
        return await super().replace_wrap(secret_id, wrapped_dek, wrap_nonce, key_id)


@pytest.mark.asyncio
async def test_rewrap_can_be_interrupted_and_resumed() -> None:
    store = InterruptAfterOne()
    workspace_id = uuid4()
    store.memberships[(workspace_id, PLATFORM.id)] = Role.WORKSPACE_ADMIN
    creator = SecretService(store, KekSettings(current=TEST_KEK, current_id="v1"))
    first = await creator.create(
        PLATFORM, workspace_id, "one", SecretScope.PLATFORM, "alpha", "req-1"
    )
    second = await creator.create(
        PLATFORM, workspace_id, "two", SecretScope.PLATFORM, "beta", "req-2"
    )
    rotator = SecretService(
        store,
        KekSettings(
            current=OTHER_KEK,
            current_id="v2",
            previous=TEST_KEK,
            previous_id="v1",
        ),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        await rotator.rewrap(PLATFORM, workspace_id, "req-rewrap-1")

    key_ids = {store.records[first.id].key_id, store.records[second.id].key_id}
    assert key_ids == {"v1", "v2"}

    result = await rotator.rewrap(PLATFORM, workspace_id, "req-rewrap-2")
    assert result.processed == 1
    assert result.remaining == 0
    assert result.unrecoverable == 0
    assert result.current_key_id == "v2"
    assert store.records[first.id].key_id == "v2"
    assert store.records[second.id].key_id == "v2"
    assert unseal(store.records[first.id].envelope(), decode_kek(OTHER_KEK)) == b"alpha"
    assert unseal(store.records[second.id].envelope(), decode_kek(OTHER_KEK)) == b"beta"


@pytest.mark.asyncio
async def test_a_workspace_admin_cannot_rewrap() -> None:
    store, service, workspace_id = _service()
    await service.create(
        ADMIN, workspace_id, "openai", SecretScope.WORKSPACE, "value", "req-1"
    )
    rotator = SecretService(
        store,
        KekSettings(
            current=OTHER_KEK, current_id="v2", previous=TEST_KEK, previous_id="v1"
        ),
    )
    with pytest.raises(ForbiddenSecretAction):
        await rotator.rewrap(ADMIN, workspace_id, "req-rewrap")


@pytest.mark.asyncio
async def test_rewrap_without_the_previous_kek_is_refused() -> None:
    store, _, workspace_id = _service()
    rotator = SecretService(store, KekSettings(current=TEST_KEK, current_id="v1"))
    with pytest.raises(PreviousKekMissing):
        await rotator.rewrap(PLATFORM, workspace_id, "req-rewrap")


@pytest.mark.asyncio
async def test_a_secret_the_previous_kek_cannot_open_is_counted_not_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`remaining` alone cannot say whether a rerun would help.

    A rotation that reports the same `remaining` every time reads as "keep
    going" when it may mean "this ciphertext is not coming back". The count
    separates the two, and the log names the record — never the plaintext,
    which is precisely what nobody can produce.
    """
    store = MemorySecretStore()
    workspace_id = uuid4()
    store.memberships[(workspace_id, PLATFORM.id)] = Role.WORKSPACE_ADMIN
    creator = SecretService(store, KekSettings(current=TEST_KEK, current_id="v1"))
    good = await creator.create(
        PLATFORM, workspace_id, "good", SecretScope.PLATFORM, "alpha", "req-1"
    )
    # Sealed under a KEK the rotation will never be given, but labelled `v1`
    # like the rest: the shape of a key that was lost, or rotated out of
    # order. The loop will reach this record and the previous KEK will not
    # open it.
    stranger = SecretService(store, KekSettings(current=THIRD_KEK, current_id="v1"))
    doomed = await stranger.create(
        PLATFORM, workspace_id, "doomed", SecretScope.PLATFORM, "gamma", "req-2"
    )

    rotator = SecretService(
        store,
        KekSettings(
            current=OTHER_KEK, current_id="v2", previous=TEST_KEK, previous_id="v1"
        ),
    )
    with caplog.at_level("ERROR"):
        result = await rotator.rewrap(PLATFORM, workspace_id, "req-rewrap")

    assert result.processed == 1
    assert result.unrecoverable == 1
    assert result.remaining == 1
    assert store.records[good.id].key_id == "v2"
    assert store.records[doomed.id].key_id == "v1"
    assert "cannot be unwrapped" in caplog.text
    assert str(doomed.id) in str(caplog.records[-1].__dict__.get("secret_id", ""))
    assert "gamma" not in caplog.text
