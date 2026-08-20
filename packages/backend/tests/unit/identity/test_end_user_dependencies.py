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
from tiny_hermes.identity.application.end_user_service import EndUserIdentityService
from tiny_hermes.identity.presentation.end_user_dependencies import (
    EndUserCaller,
    console_forbidden,
    reject_end_user_caller,
    resolve_end_user_caller,
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

    assert caller == EndUserCaller(exchanged.end_user_id, WORKSPACE_ID)


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
