"""`resolve_end_user_caller` and `reject_end_user_caller`.

Design §3's checklist calls these out by name, separate from
`resolve_workspace_caller`: a missing session behaves like every other
"nobody is signed in" 401 in this codebase, and the console guard is proven
here at the function level — `test_end_user_sessions.py` proves it again
over real HTTP against three real console routes, but the unit here is what
makes a red test fail for the right reason if the guard's own logic ever
changes rather than only when the wiring in `app.py` does.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from starlette.datastructures import Headers
from tiny_hermes.identity.application.end_user_service import EndUserIdentityService
from tiny_hermes.identity.presentation.end_user_dependencies import (
    EndUserCaller,
    console_forbidden,
    cross_origin_forbidden,
    enforce_end_user_origin,
    reject_end_user_caller,
    resolve_end_user_caller,
    resolve_end_user_caller_for_write,
)
from tiny_hermes.shared.errors import AppError

from .test_end_user_service import FakeEndUserStore, FakeKeySource, claims, rs256

WORKSPACE_ID = uuid4()


async def test_no_cookie_is_unauthenticated() -> None:
    service = EndUserIdentityService(
        FakeEndUserStore(), FakeKeySource(), session_ttl=timedelta(hours=8)
    )

    with pytest.raises(AppError) as excinfo:
        await resolve_end_user_caller(service, None)

    assert excinfo.value.status == 401


async def test_an_unknown_token_is_unauthenticated() -> None:
    service = EndUserIdentityService(
        FakeEndUserStore(), FakeKeySource(), session_ttl=timedelta(hours=8)
    )

    with pytest.raises(AppError) as excinfo:
        await resolve_end_user_caller(service, "not-a-real-session-token")

    assert excinfo.value.status == 401


async def test_a_valid_session_resolves_to_the_end_user_who_owns_it() -> None:
    store = FakeEndUserStore()
    store.register(workspace_id=WORKSPACE_ID)
    service = EndUserIdentityService(store, FakeKeySource(), session_ttl=timedelta(hours=8))
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    token = rs256(claims(aud=str(WORKSPACE_ID)))
    exchanged = await service.exchange(token, WORKSPACE_ID, now, "req-1")

    caller = await resolve_end_user_caller(service, exchanged.session_token)

    assert caller == EndUserCaller(exchanged.end_user_id, WORKSPACE_ID, ("support-bot",))


def test_no_end_user_cookie_passes_through() -> None:
    reject_end_user_caller(None)


def test_an_end_user_cookie_is_forbidden_regardless_of_validity() -> None:
    """Presence alone is enough (module docstring): a console route must
    refuse an end user who is holding this cookie at all, not only one whose
    session happens to still be live — checking liveness would need a
    database round trip on every console request just to decide whether to
    say no."""
    with pytest.raises(AppError) as excinfo:
        reject_end_user_caller("garbage-not-a-real-session-token")

    assert excinfo.value.status == 403
    assert excinfo.value.code == console_forbidden().code


# -- design §7: origin enforcement for state-changing end-user requests -----


async def test_a_write_with_no_origin_or_referer_is_not_refused_for_its_origin() -> None:
    """Design §7's check only fires on a header a browser actually sent.
    Nothing here is evidence of a cross-site request — refusing it anyway
    would mean requiring a header no legitimate client is guaranteed to
    send, on top of the cookie this codebase already treats as sufficient
    proof of a live session."""
    service = EndUserIdentityService(
        FakeEndUserStore(), FakeKeySource(), session_ttl=timedelta(hours=8)
    )

    await enforce_end_user_origin(service, WORKSPACE_ID, Headers({}))


async def test_a_write_from_a_registered_origin_is_allowed() -> None:
    store = FakeEndUserStore()
    store.register(workspace_id=WORKSPACE_ID, allowed_origins=("https://acme.example",))
    service = EndUserIdentityService(store, FakeKeySource(), session_ttl=timedelta(hours=8))

    await enforce_end_user_origin(
        service, WORKSPACE_ID, Headers({"origin": "https://acme.example"})
    )


async def test_a_write_from_an_unregistered_origin_is_refused() -> None:
    """The brief's own line: a third-party page must not be able to submit a
    Run, answer a confirmation, or erase an end user's data just because it
    can make the browser attach the `SameSite=None` cookie."""
    store = FakeEndUserStore()
    store.register(workspace_id=WORKSPACE_ID, allowed_origins=("https://acme.example",))
    service = EndUserIdentityService(store, FakeKeySource(), session_ttl=timedelta(hours=8))

    with pytest.raises(AppError) as excinfo:
        await enforce_end_user_origin(
            service, WORKSPACE_ID, Headers({"origin": "https://evil.example"})
        )

    assert excinfo.value.status == 403
    assert excinfo.value.code == cross_origin_forbidden().code


async def test_referer_is_read_when_origin_is_absent() -> None:
    """A cross-origin form POST from an older client carries `Referer` and
    no `Origin` — design §7 names both headers, not just the first."""
    store = FakeEndUserStore()
    store.register(workspace_id=WORKSPACE_ID, allowed_origins=("https://acme.example",))
    service = EndUserIdentityService(store, FakeKeySource(), session_ttl=timedelta(hours=8))

    with pytest.raises(AppError) as excinfo:
        await enforce_end_user_origin(
            service,
            WORKSPACE_ID,
            Headers({"referer": "https://evil.example/attack.html"}),
        )

    assert excinfo.value.status == 403


async def test_a_disabled_issuers_origins_no_longer_count() -> None:
    """`ChannelIssuerRow.allowed_origins`' own docstring: disabling an
    issuer is documented to take effect for new credentials immediately
    (§4.3). An origin only a disabled row still names should not go on being
    trusted just because nothing revoked it explicitly."""
    from tiny_hermes.identity.domain.models import ChannelIssuerStatus

    store = FakeEndUserStore()
    store.register(
        workspace_id=WORKSPACE_ID,
        allowed_origins=("https://acme.example",),
        status=ChannelIssuerStatus.DISABLED,
    )
    service = EndUserIdentityService(store, FakeKeySource(), session_ttl=timedelta(hours=8))

    with pytest.raises(AppError) as excinfo:
        await enforce_end_user_origin(
            service, WORKSPACE_ID, Headers({"origin": "https://acme.example"})
        )

    assert excinfo.value.status == 403


async def test_resolve_for_write_checks_origin_only_after_the_session_is_proven() -> None:
    """An invalid session must fail as unauthenticated, not as a cross-origin
    refusal — the latter would hand an unauthenticated prober a way to learn
    which origins are registered (design §8's enumeration concern, applied
    here the same way it already is to issuer lookups)."""
    service = EndUserIdentityService(
        FakeEndUserStore(), FakeKeySource(), session_ttl=timedelta(hours=8)
    )

    with pytest.raises(AppError) as excinfo:
        await resolve_end_user_caller_for_write(
            service, None, Headers({"origin": "https://evil.example"})
        )

    assert excinfo.value.status == 401


async def test_resolve_for_write_refuses_a_live_session_from_a_bad_origin() -> None:
    store = FakeEndUserStore()
    store.register(workspace_id=WORKSPACE_ID, allowed_origins=("https://acme.example",))
    service = EndUserIdentityService(store, FakeKeySource(), session_ttl=timedelta(hours=8))
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    token = rs256(claims(aud=str(WORKSPACE_ID)))
    exchanged = await service.exchange(token, WORKSPACE_ID, now, "req-1")

    with pytest.raises(AppError) as excinfo:
        await resolve_end_user_caller_for_write(
            service,
            exchanged.session_token,
            Headers({"origin": "https://evil.example"}),
        )

    assert excinfo.value.status == 403
    assert excinfo.value.code == cross_origin_forbidden().code
