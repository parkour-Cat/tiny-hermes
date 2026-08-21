"""§5, wired to real HTTP: the doors an end user reaches to run an Agent and
watch it work.

Everything upstream of this module — the credential exchange
(`identity/presentation/end_user_routes.py`), the two-gate resolution
(`AgentCatalog.resolve_end_user_agent`), the widened `CallerType` — existed
before this task and was reachable by nothing. This is where it gets called.

Deliberately its own router and its own two endpoints, never
`session_router`/`run_router` (`runs/presentation/routes.py`) with a second
auth branch bolted on. Those are `_CONSOLE_ONLY` in `api/app.py`, and design
§4.5's first sentence is that an end user gets 403 from every console
endpoint with no exceptions — a shared router would mean either weakening
that guard for these two routes or teaching `resolve_workspace_caller` a
third kind of caller it was never written for (the same reasoning
`resolve_end_user_caller`'s own docstring gives for staying out of that
function). So this router is never given `_CONSOLE_ONLY`, and every route in
it authenticates with `resolve_end_user_caller` and nothing else — both
routes here are state-changing, so in practice that means `resolve_end_user_
caller_for_write`, which adds design §7's origin check on top: the cookie
that authenticates these routes is `SameSite=None; Secure` and there is no
`X-CSRF-Token` to fall back on, so the request's own `Origin`/`Referer` is
what stands in for one.

**The two refusals stay distinguishable here, not just in the domain.** A
credential that named an alias the workspace never turned on (`EndUser
AccessGateClosed`) gets a 403 that names the alias — the workspace admin's
problem, and they can only fix what they can see. A credential that never
named the alias at all (`EndUserAccessNotAssigned`) gets a 403 built from a
fixed string, never from the exception's own message — that message embeds
the alias for whoever reads server-side logs, and design §8's refusal table
is explicit that an end user calling an unassigned Agent must not learn
anything past "no". Passing `str(error)` here would undo that on the first
edit to the exception's wording.

**The two GET routes are not in design §5.** They fill a gap the design left
open: an end user could start a Session and submit a Run, but nothing built
for them could read either one back — `session_router`/`run_router` are
`_CONSOLE_ONLY`, same as every other console router. Without some door back
onto a Run's outcome, a chat surface has nothing to show after "sent". Both
routes reuse `RunCoordination.get_end_user_session`/`get_end_user_run`,
which enforce the exact ownership rule `submit_end_user_run` already
enforces on the write — a guessed id opens nobody's conversation but the
asker's own — so this is the write path's existing rule, read twice more,
not a second rule invented for reads. Both are GETs, so neither uses
`resolve_end_user_caller_for_write`: design §7's origin check exists for
state-changing requests, and a read that only ever returns this end user's
own data to them is not the shape that check defends against.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
from pydantic import BaseModel, Field

from tiny_hermes.agents.application.service import (
    AgentCatalog,
    EndUserAccessGateClosed,
    EndUserAccessNotAssigned,
)
from tiny_hermes.agents.domain.models import Agent, AgentVersion
from tiny_hermes.api.resources import ApplicationResources
from tiny_hermes.identity.application.end_user_service import EndUserIdentityService
from tiny_hermes.identity.presentation.end_user_dependencies import (
    END_USER_SESSION_COOKIE,
    EndUserCaller,
    resolve_end_user_caller,
    resolve_end_user_caller_for_write,
)
from tiny_hermes.runs.application.service import RunCoordination, RunCoordinationError
from tiny_hermes.runs.domain.models import CanonicalMessage, RunSnapshot, SessionMode
from tiny_hermes.runs.presentation.errors import as_app_error
from tiny_hermes.runs.presentation.routes import REPLAYED_HEADER, QueueResponse, SessionResponse
from tiny_hermes.shared.errors import AppError

EndUserSessionCookie = Annotated[str | None, Cookie(alias=END_USER_SESSION_COOKIE)]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]


class CreateEndUserSessionRequest(BaseModel):
    session_mode: SessionMode = SessionMode.PERSISTENT


class CreateEndUserRunRequest(BaseModel):
    input: str = Field(min_length=1, max_length=32_768)


class EndUserRunResponse(BaseModel):
    """Task-7 review finding 4: the console's own `RunResponse` carries the
    platform's operational document for a Run — budget consumption,
    checkpoint replay/effect/usage internals, a goal round's outcome — none
    of which is this surface's business to hand over. An end user is
    somebody else's employee, not a console operator, and reusing the
    console's shape verbatim leaked those fields to them by omission of a
    decision, not because anyone chose to share them.

    This carries only what `ChatPage.tsx` actually reads: which Run this
    is, which Session it belongs to, whether it is still going, and where
    it sits in the queue (`queue.status === "session_blocked"` is what
    drives the composer's "still busy" banner).
    """

    id: UUID
    session_id: UUID
    status: str
    finished_at: datetime | None
    queue: QueueResponse

    @classmethod
    def from_domain(cls, run: RunSnapshot) -> "EndUserRunResponse":
        return cls.model_validate(run.document())


class EndUserSessionMessageResponse(BaseModel):
    """Kept apart from the console's `SessionMessageResponse`, even though
    the two carry the same fields today (finding 4 of the task-7 review):
    a console-only field added to that model later — the way `author`
    itself was added to it — should need a deliberate decision to widen
    this one too, not reach an end user by simply being on the model this
    route happened to import.
    """

    role: str
    parts: list[dict[str, Any]]
    author: str | None = None

    @classmethod
    def from_domain(cls, message: CanonicalMessage) -> "EndUserSessionMessageResponse":
        return cls.model_validate(message.document())


def end_user_run_router(resources: ApplicationResources) -> APIRouter:
    router = APIRouter(prefix="/api/v1/end-user", tags=["end-user-runs"])
    identity_dependency = resources.end_user_identity_service
    catalog_dependency = resources.agent_catalog
    runs_dependency = resources.run_coordination

    @router.post(
        "/agents/{alias}/sessions",
        response_model=SessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_session(  # pyright: ignore[reportUnusedFunction]
        alias: str,
        payload: CreateEndUserSessionRequest,
        request: Request,
        identity: Annotated[
            EndUserIdentityService, Depends(identity_dependency, scope="function")
        ],
        catalog: Annotated[AgentCatalog, Depends(catalog_dependency, scope="function")],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        end_user_session: EndUserSessionCookie = None,
    ) -> SessionResponse:
        caller = await resolve_end_user_caller_for_write(
            identity, end_user_session, request.headers
        )
        agent, _version = await _resolve_agent(catalog, caller, alias)
        created = await runs.create_end_user_session(
            caller.workspace_id,
            caller.end_user_id,
            agent.id,
            payload.session_mode,
            request.state.request_id,
        )
        return SessionResponse.from_domain(created)

    @router.post(
        "/sessions/{session_id}/runs",
        response_model=EndUserRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_run(  # pyright: ignore[reportUnusedFunction]
        session_id: UUID,
        payload: CreateEndUserRunRequest,
        request: Request,
        response: Response,
        identity: Annotated[
            EndUserIdentityService, Depends(identity_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        idempotency_key: IdempotencyHeader = None,
        end_user_session: EndUserSessionCookie = None,
    ) -> EndUserRunResponse:
        caller = await resolve_end_user_caller_for_write(
            identity, end_user_session, request.headers
        )
        try:
            accepted = await runs.submit_end_user_run(
                caller.workspace_id,
                caller.end_user_id,
                session_id,
                payload.input,
                idempotency_key,
                request.state.request_id,
            )
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        if accepted.replayed:
            response.status_code = status.HTTP_200_OK
            response.headers[REPLAYED_HEADER] = "true"
        else:
            response.status_code = status.HTTP_201_CREATED
            await resources.wake_up_notifier().publish(caller.workspace_id, accepted.run_id)
        return EndUserRunResponse.model_validate(accepted.document)

    @router.get(
        "/sessions/{session_id}/messages",
        response_model=list[EndUserSessionMessageResponse],
    )
    async def list_session_messages(  # pyright: ignore[reportUnusedFunction]
        session_id: UUID,
        identity: Annotated[
            EndUserIdentityService, Depends(identity_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        end_user_session: EndUserSessionCookie = None,
    ) -> list[EndUserSessionMessageResponse]:
        caller = await resolve_end_user_caller(identity, end_user_session)
        try:
            messages = await runs.read_end_user_session_messages(
                caller.workspace_id, caller.end_user_id, session_id
            )
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        return [EndUserSessionMessageResponse.from_domain(item) for item in messages]

    @router.get("/runs/{run_id}", response_model=EndUserRunResponse)
    async def get_run(  # pyright: ignore[reportUnusedFunction]
        run_id: UUID,
        identity: Annotated[
            EndUserIdentityService, Depends(identity_dependency, scope="function")
        ],
        runs: Annotated[RunCoordination, Depends(runs_dependency, scope="function")],
        end_user_session: EndUserSessionCookie = None,
    ) -> EndUserRunResponse:
        caller = await resolve_end_user_caller(identity, end_user_session)
        try:
            found = await runs.get_end_user_run(caller.workspace_id, caller.end_user_id, run_id)
        except RunCoordinationError as error:
            raise as_app_error(error) from error
        return EndUserRunResponse.from_domain(found)

    return router


async def _resolve_agent(
    catalog: AgentCatalog, caller: EndUserCaller, alias: str
) -> tuple[Agent, AgentVersion]:
    try:
        return await catalog.resolve_end_user_agent(caller.workspace_id, alias, caller.agents)
    except EndUserAccessGateClosed as error:
        raise AppError(
            code="end_user_access_gate_closed",
            title="Agent not available",
            status=403,
            detail=f"end-user access is not enabled for {error.alias}",
        ) from error
    except EndUserAccessNotAssigned as error:
        # The exception's own message embeds the alias (see its docstring);
        # this detail deliberately does not, matching design §8: an end user
        # calling an Agent nobody assigned them learns nothing past "no".
        raise AppError(
            code="end_user_agent_not_found",
            title="Agent not found",
            status=403,
            detail="No Agent by that name is available to you.",
        ) from error
