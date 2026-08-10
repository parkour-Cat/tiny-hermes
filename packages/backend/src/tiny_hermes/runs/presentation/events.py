"""The resumable Run Event stream.

Two things here deliberately break the header conventions the rest of the API
follows, because a browser ``EventSource`` cannot set request headers: the
workspace arrives as a query parameter instead of ``X-Workspace-Id``, and the
cursor is accepted as a query parameter as well as the standard
``Last-Event-ID``. The session cookie still authenticates, membership and
ownership are still checked before a single frame is written, and a
cross-workspace identifier still returns a generic ``404``.

The loop polls committed rows rather than subscribing to the wake-up channel.
A Redis subscription is a per-connection resource and the application's single
shared subscription is not safe for concurrent readers, which is a real cost
to pay for at most half a second of delivery latency.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Header, Request
from fastapi.responses import StreamingResponse

from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.presentation.dependencies import (
    SESSION_COOKIE,
    authenticate_browser_user,
)
from tiny_hermes.runs.application.service import (
    RunCoordinationError,
    RunEventCursorTooOld,
)
from tiny_hermes.runs.ports.store import RunEventRecord, RunEventWindow
from tiny_hermes.runs.presentation.errors import actor_of, as_app_error
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor

CursorHeader = Annotated[str | None, Header(alias="Last-Event-ID")]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]

BATCH = 200
POLL_SECONDS = 0.5


def run_event_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/runs", tags=["runs"])
    auth_scope = asynccontextmanager(resources.auth_service)
    runs_scope = asynccontextmanager(resources.run_coordination)

    async def _poll(
        workspace_id: UUID, actor: Actor, run_id: UUID, cursor: int
    ) -> tuple[Sequence[RunEventRecord], RunEventWindow]:
        async with runs_scope() as runs:
            records = await runs.read_events(workspace_id, actor, run_id, cursor, BATCH)
            return records, await runs.event_window(workspace_id, actor, run_id)

    async def _frames(
        request: Request, workspace_id: UUID, actor: Actor, run_id: UUID, after: int
    ) -> AsyncIterator[bytes]:
        """Yield committed events until the Run ends or the subscriber leaves.

        Every poll opens and closes its own session, so a subscriber that stays
        connected for an hour never pins a database connection. The poll is
        shielded because a subscriber can disconnect mid-read, and a read
        cancelled between its query and its commit would strand the connection
        instead of returning it to the pool.
        """
        heartbeat_seconds = resources.settings.sse_heartbeat_seconds
        cursor = after
        quiet_since = monotonic()
        while not await request.is_disconnected():
            records, window = await asyncio.shield(
                _poll(workspace_id, actor, run_id, cursor)
            )
            for record in records:
                cursor = record.sequence
                yield _frame(record)
            if records:
                quiet_since = monotonic()
                continue
            if window.is_terminal and cursor + 1 >= window.next_sequence:
                return
            if monotonic() - quiet_since >= heartbeat_seconds:
                quiet_since = monotonic()
                yield b": heartbeat\n\n"
            await asyncio.sleep(POLL_SECONDS)

    @router.get("/{run_id}/events")
    async def stream_run_events(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        request: Request,
        workspace_id: str | None = None,
        last_event_id: str | None = None,
        cursor_header: CursorHeader = None,
        session_token: SessionCookie = None,
    ) -> StreamingResponse:
        async with auth_scope() as auth:
            user = await authenticate_browser_user(auth, session_token)
        selected = _selected_workspace(workspace_id)
        actor = actor_of(user)
        after = _cursor(cursor_header, last_event_id)
        try:
            async with runs_scope() as runs:
                await runs.open_event_stream(selected, actor, run_id, after)
        except RunCoordinationError as error:
            raise _as_stream_error(error, run_id) from error
        return StreamingResponse(
            _frames(request, selected, actor, run_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


def _frame(record: RunEventRecord) -> bytes:
    data = json.dumps(
        {
            "sequence": record.sequence,
            "event_type": record.event_type.value,
            "occurred_at": record.occurred_at.isoformat(),
            "payload": record.payload,
        },
        separators=(",", ":"),
    )
    return (
        f"id: {record.sequence}\n"
        f"event: {record.event_type.value}\n"
        f"data: {data}\n\n"
    ).encode()


def _cursor(header_value: str | None, query_value: str | None) -> int:
    """``Last-Event-ID`` wins, because a reconnecting EventSource resends it."""
    for candidate in (header_value, query_value):
        if candidate is None or not candidate.strip():
            continue
        try:
            return max(int(candidate), 0)
        except ValueError:
            return 0
    return 0


def _selected_workspace(raw_value: str | None) -> UUID:
    if raw_value is None:
        raise AppError(
            code="workspace_required",
            title="Workspace required",
            status=400,
            detail="A workspace_id query parameter is required for this stream.",
        )
    try:
        return UUID(raw_value)
    except ValueError as error:
        raise AppError(
            code="invalid_workspace_id",
            title="Invalid workspace identifier",
            status=400,
            detail="workspace_id must be a UUID.",
        ) from error


def _as_stream_error(error: RunCoordinationError, run_id: UUID) -> AppError:
    if isinstance(error, RunEventCursorTooOld):
        return AppError(
            code="event_cursor_too_old",
            title="Event cursor too old",
            status=410,
            detail="Re-read the run snapshot before resubscribing to its events.",
            context={
                "earliest_available_sequence": error.earliest_available_sequence,
                "run_url": f"/api/v1/runs/{run_id}",
            },
        )
    return as_app_error(error)
