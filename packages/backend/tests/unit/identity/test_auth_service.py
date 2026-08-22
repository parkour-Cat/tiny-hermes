from dataclasses import replace

import pytest
from tiny_hermes.identity.application.auth_service import (
    AuthService,
    BootstrapClosed,
    InvalidBootstrapToken,
    InvalidCredentials,
)
from tiny_hermes.identity.domain.models import NewLocalUser
from tiny_hermes.identity.infrastructure.memory_store import MemoryAuthStore


@pytest.mark.asyncio
async def test_bootstrap_creates_first_platform_admin_and_then_closes() -> None:
    store = MemoryAuthStore()
    service = AuthService(store, bootstrap_token="a" * 32, session_ttl_seconds=28_800)
    command = NewLocalUser(
        subject="admin@example.com", display_name="Admin", password="long-pass-123"
    )

    first = await service.bootstrap("a" * 32, command, request_id="req-1")

    assert first.is_platform_admin is True
    assert store.audit_actions == ["identity.bootstrap_succeeded"]
    with pytest.raises(BootstrapClosed):
        await service.bootstrap(
            "a" * 32,
            replace(command, subject="other@example.com"),
            "req-2",
        )


@pytest.mark.asyncio
async def test_login_returns_raw_tokens_but_store_keeps_only_digests() -> None:
    store = MemoryAuthStore()
    service = AuthService(store, bootstrap_token="a" * 32, session_ttl_seconds=28_800)
    command = NewLocalUser("admin@example.com", "Admin", "long-pass-123")
    await service.bootstrap("a" * 32, command, "req-1")

    session_token, csrf_token, user = await service.login(
        " ADMIN@example.com ", "long-pass-123", "req-2"
    )

    assert user.subject == "admin@example.com"
    assert session_token not in store.session_token_digests
    assert csrf_token not in store.csrf_token_digests
    assert service.digest_token(session_token) in store.session_token_digests
    assert service.digest_token(csrf_token) in store.csrf_token_digests
    assert store.audit_actions[-1] == "identity.login_succeeded"


@pytest.mark.asyncio
async def test_login_rejects_wrong_password_without_creating_session() -> None:
    store = MemoryAuthStore()
    service = AuthService(store, bootstrap_token="a" * 32, session_ttl_seconds=28_800)
    await service.bootstrap(
        "a" * 32,
        NewLocalUser("admin@example.com", "Admin", "long-pass-123"),
        "req-1",
    )

    with pytest.raises(InvalidCredentials):
        await service.login("admin@example.com", "wrong-password", "req-2")

    assert store.session_token_digests == set()
    assert store.audit_actions[-1] == "identity.login_failed"


@pytest.mark.asyncio
async def test_bootstrap_rejects_wrong_token_and_records_denial() -> None:
    store = MemoryAuthStore()
    service = AuthService(store, bootstrap_token="a" * 32, session_ttl_seconds=28_800)

    with pytest.raises(InvalidBootstrapToken):
        await service.bootstrap(
            "b" * 32,
            NewLocalUser("admin@example.com", "Admin", "long-pass-123"),
            "req-1",
        )

    assert await store.has_platform_admin() is False
    assert store.audit_actions == ["identity.bootstrap_failed"]


@pytest.mark.asyncio
async def test_login_refuses_a_local_identity_with_no_password_hash() -> None:
    """OIDC login design's one schema change made `password_hash` nullable.
    Nothing in the real bootstrap path can produce a `local` identity with a
    `None` hash — `NewLocalUser.password` is required — but this pins that
    `AuthService.login` refuses one anyway rather than asking `pwdlib.verify`
    to check a password against nothing, which is exactly the kind of gap
    that turns into an auth bypass if `verify(password, None)` ever changes
    its mind about raising."""
    store = MemoryAuthStore()
    service = AuthService(store, bootstrap_token="a" * 32, session_ttl_seconds=28_800)
    store.seed_local_identity("nopass@example.com", password_hash=None)

    with pytest.raises(InvalidCredentials):
        await service.login("nopass@example.com", "anything-at-all", "req-1")

    assert store.session_token_digests == set()
    assert store.audit_actions[-1] == "identity.login_failed"


@pytest.mark.asyncio
async def test_find_or_create_oidc_identity_creates_a_new_user_on_first_sight() -> None:
    store = MemoryAuthStore()
    service = AuthService(store, bootstrap_token="a" * 32, session_ttl_seconds=28_800)

    user = await service.find_or_create_oidc_identity("idp-sub-1", "Ada", "req-1")

    assert user.subject == "idp-sub-1"
    assert user.is_platform_admin is False
    assert store.audit_actions == ["identity.oidc_user_created"]


@pytest.mark.asyncio
async def test_find_or_create_oidc_identity_returns_the_same_user_on_a_second_sight() -> None:
    store = MemoryAuthStore()
    service = AuthService(store, bootstrap_token="a" * 32, session_ttl_seconds=28_800)
    first = await service.find_or_create_oidc_identity("idp-sub-1", "Ada", "req-1")

    second = await service.find_or_create_oidc_identity("idp-sub-1", "Ada Renamed", "req-2")

    assert second.id == first.id
    # No second "user created" audit line, and no attempt to rename on sight.
    assert store.audit_actions == ["identity.oidc_user_created"]


@pytest.mark.asyncio
async def test_authenticate_csrf_and_logout_follow_session_state() -> None:
    store = MemoryAuthStore()
    service = AuthService(store, bootstrap_token="a" * 32, session_ttl_seconds=28_800)
    await service.bootstrap(
        "a" * 32,
        NewLocalUser("admin@example.com", "Admin", "long-pass-123"),
        "req-1",
    )
    session_token, csrf_token, user = await service.login(
        "admin@example.com", "long-pass-123", "req-2"
    )

    assert await service.authenticate(session_token) == user
    assert await service.verify_csrf(session_token, csrf_token) == user
    with pytest.raises(InvalidCredentials):
        await service.verify_csrf(session_token, "wrong-csrf-token")

    await service.logout(session_token, "req-3")

    with pytest.raises(InvalidCredentials):
        await service.authenticate(session_token)
    assert store.audit_actions[-1] == "identity.logout_succeeded"
