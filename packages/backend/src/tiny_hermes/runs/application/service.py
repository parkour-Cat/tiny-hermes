from collections.abc import Sequence
from typing import Any
from uuid import UUID

from tiny_hermes.runs.domain.event_cursor import cursor_is_stale
from tiny_hermes.runs.domain.models import (
    CallerIdentity,
    CallerType,
    CanonicalMessage,
    DeliveryMode,
    RunCapabilities,
    RunSignal,
    RunSnapshot,
    SessionMode,
    SessionSnapshot,
    TextBlock,
    WorkspaceUsageSummary,
    fingerprint_request,
)
from tiny_hermes.runs.ports.store import (
    AcceptedRun,
    AcceptRunCommand,
    ApplySignalCommand,
    ClaimedRun,
    ClaimRunCommand,
    ControlRunCommand,
    CreateSessionCommand,
    RepairResult,
    RetryRunCommand,
    RunEventRecord,
    RunEventWindow,
    RunStore,
    WidenBudgetCommand,
)
from tiny_hermes.shared.errors import AuditedDenial
from tiny_hermes.tenancy.domain.models import Actor, Role

WRITERS = {Role.WORKSPACE_ADMIN, Role.DEVELOPER}
READERS = {Role.WORKSPACE_ADMIN, Role.DEVELOPER, Role.VIEWER}
#: Product design §12.3 wants an *authorized* subject for a budget widening,
#: which is a narrower question than who may run work. A developer starts and
#: pauses Runs; deciding that this workspace will spend more than it agreed to
#: is the workspace administrator's.
BUDGET_HOLDERS = {Role.WORKSPACE_ADMIN}

RUNS_ENDPOINT = "POST /api/v1/runs"
RETRY_ENDPOINT = "POST /api/v1/runs/{run_id}/retry"
#: A separate idempotency/fingerprint namespace from `RUNS_ENDPOINT`, on
#: purpose (§5). The two are reached by different subjects through different
#: authorization — a workspace Role for one, the two-gate Agent check for the
#: other — and collapsing them into one endpoint string would let a replayed
#: request from one path be satisfied by a row the other path wrote.
END_USER_RUNS_ENDPOINT = "POST /api/v1/end-user/sessions/{session_id}/runs"


class RunCoordinationError(Exception):
    """Base class for every expected Run Coordination refusal."""


class ForbiddenRunAction(RunCoordinationError):
    """`recorded` says a security audit row was written before this was
    raised, and therefore that the transaction must be committed rather than
    rolled back.

    Carried on the exception rather than decided at the HTTP boundary,
    because only the raising site knows whether it wrote one — and a
    boundary that guessed would either lose real refusal records or commit
    transactions that wrote nothing. Default `False`: the ordinary 403 has
    nothing to keep.
    """

    def __init__(self, *, recorded: bool = False) -> None:
        super().__init__()
        self.recorded = recorded


class UnknownSession(RunCoordinationError):
    pass


class UnknownRun(RunCoordinationError):
    pass


class SessionAgentNotFound(RunCoordinationError):
    pass


class AgentNotPublished(RunCoordinationError):
    pass


class IdempotencyKeyRequired(RunCoordinationError):
    pass


class IdempotencyKeyReused(RunCoordinationError):
    pass


class EventSequenceConflict(RunCoordinationError):
    pass


class StateVersionConflict(RunCoordinationError):
    pass


class RunEventCursorTooOld(RunCoordinationError):
    """The subscriber's cursor is below the events the platform still retains."""

    def __init__(self, earliest_available_sequence: int) -> None:
        super().__init__(str(earliest_available_sequence))
        self.earliest_available_sequence = earliest_available_sequence


class LeaseLost(RunCoordinationError):
    """The lease this writer held was released, reclaimed, or superseded."""


class DeniedRunControl(RunCoordinationError, AuditedDenial):
    """An illegal control whose audit denial must survive the refusal."""

    def __init__(self, code: str = "invalid_state_transition") -> None:
        super().__init__(code)
        self.code = code


class RetryNotSafe(RunCoordinationError):
    pass


class RetryContextStale(RunCoordinationError):
    pass


class RetryBudgetExhausted(RunCoordinationError):
    pass


class RetryLimitReached(RunCoordinationError):
    pass


class BudgetNotWidened(RunCoordinationError):
    """The value asked for is not above the ceiling already in force.

    Refused rather than accepted as a no-op: an operator who meant to raise a
    ceiling and typed the number already there should find that out now, not
    when the Run stops at the same place a second time.
    """


class RunCoordination:
    """Session and Run rules.

    The service resolves who the caller is and what they may do, then hands one
    whole business command to the store. It never chooses a Run state.
    """

    def __init__(self, store: RunStore) -> None:
        self._store = store

    async def create_session(
        self,
        workspace_id: UUID,
        actor: Actor,
        agent_id: UUID,
        session_mode: SessionMode,
        request_id: str,
    ) -> SessionSnapshot:
        await self._require_role(workspace_id, actor, WRITERS)
        return await self._store.create_session(
            CreateSessionCommand(
                workspace_id=workspace_id,
                agent_id=agent_id,
                caller=_caller(actor),
                session_mode=session_mode,
                request_id=request_id,
            )
        )

    async def get_session(
        self, workspace_id: UUID, actor: Actor, session_id: UUID
    ) -> SessionSnapshot:
        await self._require_role(workspace_id, actor, READERS)
        session = await self._store.get_session(workspace_id, session_id)
        if session is None:
            raise UnknownSession
        return session

    async def create_end_user_session(
        self,
        workspace_id: UUID,
        end_user_id: UUID,
        agent_id: UUID,
        session_mode: SessionMode,
        request_id: str,
    ) -> SessionSnapshot:
        """§5's Session half, for a subject with no workspace Role at all.

        No `_require_role`: an end user is never a member, so that check has
        nothing to look up. Authorization already happened one layer up —
        the caller reaches `agent_id` only by resolving an alias through
        `AgentCatalog.resolve_end_user_agent`'s two gates — so this only
        writes what that resolution already approved. `caller_type=end_user`
        here is what makes this Session's private memory the end user's own
        (`_remembered`'s own docstring) rather than nobody's.
        """
        return await self._store.create_session(
            CreateSessionCommand(
                workspace_id=workspace_id,
                agent_id=agent_id,
                caller=CallerIdentity(CallerType.END_USER, end_user_id),
                session_mode=session_mode,
                request_id=request_id,
            )
        )

    async def list_sessions(
        self, workspace_id: UUID, actor: Actor
    ) -> Sequence[SessionSnapshot]:
        await self._require_role(workspace_id, actor, READERS)
        return await self._store.list_sessions(workspace_id)

    async def submit_run(
        self,
        workspace_id: UUID,
        actor: Actor,
        session_id: UUID,
        text: str,
        idempotency_key: str | None,
        request_id: str,
        delivery_mode: DeliveryMode | None = None,
    ) -> AcceptedRun:
        role = await self._require_role(workspace_id, actor, WRITERS)
        key = _require_idempotency_key(idempotency_key)
        message = CanonicalMessage("user", (TextBlock(text=text),))
        return await self._store.accept_run(
            AcceptRunCommand(
                workspace_id=workspace_id,
                session_id=session_id,
                caller=_caller(actor),
                capabilities=_capabilities(role),
                endpoint=RUNS_ENDPOINT,
                idempotency_key=key,
                request_fingerprint=fingerprint_request(
                    "POST", RUNS_ENDPOINT, workspace_id, session_id, message, None
                ),
                message=message,
                request_id=request_id,
                delivery_mode=None if delivery_mode is None else delivery_mode.value,
            )
        )

    async def submit_end_user_run(
        self,
        workspace_id: UUID,
        end_user_id: UUID,
        session_id: UUID,
        text: str,
        idempotency_key: str | None,
        request_id: str,
    ) -> AcceptedRun:
        """§5's Run half.

        `session_id` must be a Session this same end user started. Checked
        by `get_end_user_session` rather than left implicit: a Session's
        private-memory subject is read off *its own* `caller_type`/
        `caller_id`, never off whoever is submitting a Run against it
        (`_remembered`'s own docstring), so an end user who guessed another
        end user's `session_id` would otherwise be able to speak into that
        other subject's conversation — and have it remembered as theirs.
        The same check gates the read side (`get_end_user_run`,
        `read_end_user_session_messages`), which is why it lives in its own
        method rather than staying inline here.

        `RunCapabilities` is `can_control=True, can_retry=True`
        unconditionally: there is no workspace Role to read one off, and both
        capabilities are about this Run and nobody else's — the same reason
        `_require_role`'s WRITERS/READERS split does not apply here at all.
        """
        session = await self.get_end_user_session(workspace_id, end_user_id, session_id)
        key = _require_idempotency_key(idempotency_key)
        message = CanonicalMessage("user", (TextBlock(text=text),))
        return await self._store.accept_run(
            AcceptRunCommand(
                workspace_id=workspace_id,
                session_id=session.id,
                caller=CallerIdentity(CallerType.END_USER, end_user_id),
                capabilities=RunCapabilities(can_control=True, can_retry=True),
                endpoint=END_USER_RUNS_ENDPOINT,
                idempotency_key=key,
                request_fingerprint=fingerprint_request(
                    "POST", END_USER_RUNS_ENDPOINT, workspace_id, session_id, message, None
                ),
                message=message,
                request_id=request_id,
                delivery_mode=None,
            )
        )

    async def get_end_user_session(
        self, workspace_id: UUID, end_user_id: UUID, session_id: UUID
    ) -> SessionSnapshot:
        """The read half of `submit_end_user_run`'s own ownership check,
        pulled out so both share it rather than keeping two copies of "does
        this Session belong to this end user" in step. A Session's subject
        is read off its own `caller`, never off whoever is asking — the same
        reasoning `submit_end_user_run`'s docstring gives for why a guessed
        `session_id` must not work.
        """
        session = await self._store.get_session(workspace_id, session_id)
        if session is None:
            raise UnknownSession
        if (
            session.caller.caller_type is not CallerType.END_USER
            or session.caller.caller_id != end_user_id
        ):
            # §23 assertion 2: refused *and recorded*. Written here rather
            # than at the HTTP boundary because this is the only layer that
            # knows both who reached and whose Session they reached for —
            # `forbidden()` has neither, and an event that could not name
            # both would not answer the question it exists for.
            #
            # The reacher is the actor. An event filed under the owner would
            # put the probe in the victim's history, which is where nobody
            # searching for a probe would look.
            await self._store.record_refusal(
                workspace_id=workspace_id,
                actor_type=CallerType.END_USER.value,
                actor_id=end_user_id,
                action="end_user_session.refused",
                resource_id=session_id,
            )
            raise ForbiddenRunAction(recorded=True)
        return session

    async def read_end_user_session_messages(
        self, workspace_id: UUID, end_user_id: UUID, session_id: UUID
    ) -> Sequence[CanonicalMessage]:
        """An end user's own read of their own conversation.

        No audit row, unlike `read_session_messages`: §6's audit is the
        price of a *developer* reading somebody else's content (§4.6), and
        an owner reading their own words was never the read that price was
        for.
        """
        await self.get_end_user_session(workspace_id, end_user_id, session_id)
        return await self._store.list_session_messages(workspace_id, session_id)

    async def get_end_user_run(
        self, workspace_id: UUID, end_user_id: UUID, run_id: UUID
    ) -> RunSnapshot:
        """`can_control=True, can_retry=True` unconditionally, matching the
        capabilities `submit_end_user_run` already grants this Run — there
        is no workspace Role to read one off, and `available_actions` on a
        Run this end user is reading should reflect what they were actually
        given, not a Role that does not apply to them.

        Ownership is checked through the Run's own Session rather than a
        column on the Run itself: `RunSnapshot` carries no caller of its
        own (a Run belongs to the Session it was submitted through), so
        `get_end_user_session` is the one place that already knows how to
        answer "does this belong to this end user" and is asked again here
        rather than re-derived.
        """
        run = await self._store.get_run(
            workspace_id, run_id, RunCapabilities(can_control=True, can_retry=True)
        )
        if run is None:
            raise UnknownRun
        await self.get_end_user_session(workspace_id, end_user_id, run.session_id)
        return run

    async def cancel_end_user_run(
        self,
        workspace_id: UUID,
        end_user_id: UUID,
        run_id: UUID,
        expected_state_version: int,
        request_id: str,
    ) -> RunSnapshot:
        """Plan §10's Run half: 本人 can cancel a Run they started, once it
        has stopped anywhere they cannot otherwise reach it.

        Reuses `_store.control_run` — the same `ControlRunCommand` →
        `SqlRunStore.control_run` → `RunStateMachine` path the console's own
        `/runs/{id}/cancel` calls — rather than a second dispatcher: the
        legal-transition table is `RunStateMachine`'s territory, not this
        method's to reimplement for a second caller kind.

        No `_require_role`: an end user is never a workspace member, so
        there is no Role to read one off (`create_end_user_session`'s own
        reasoning). Ownership stands in for it instead —
        `get_end_user_run` raises before this reaches the store at all if
        `run_id` is not this end user's own, the same check the read side
        already enforces, asked again here rather than re-derived.

        Only cancel. §10 leaves pause and resume out on purpose — the plan
        itself explains why (no place in the chat UI for "pause", and
        cancel is the one action both erasure and "stop, I don't want this"
        need) — so there is no `pause_end_user_run`/`resume_end_user_run`
        beside this one to eventually add a signal to.
        """
        await self.get_end_user_run(workspace_id, end_user_id, run_id)
        return await self._store.control_run(
            ControlRunCommand(
                workspace_id=workspace_id,
                run_id=run_id,
                caller=CallerIdentity(CallerType.END_USER, end_user_id),
                capabilities=RunCapabilities(can_control=True, can_retry=True),
                signal=RunSignal.CANCEL_REQUESTED,
                expected_state_version=expected_state_version,
                request_id=request_id,
            )
        )

    async def retry_run(
        self,
        workspace_id: UUID,
        actor: Actor,
        source_run_id: UUID,
        idempotency_key: str | None,
        request_id: str,
    ) -> AcceptedRun:
        role = await self._require_role(workspace_id, actor, WRITERS)
        key = _require_idempotency_key(idempotency_key)
        return await self._store.derive_retry(
            RetryRunCommand(
                workspace_id=workspace_id,
                source_run_id=source_run_id,
                caller=_caller(actor),
                capabilities=_capabilities(role),
                endpoint=RETRY_ENDPOINT,
                idempotency_key=key,
                request_fingerprint=fingerprint_request(
                    "POST", RETRY_ENDPOINT, workspace_id, source_run_id, None, None
                ),
                request_id=request_id,
            )
        )

    async def get_run(self, workspace_id: UUID, actor: Actor, run_id: UUID) -> RunSnapshot:
        role = await self._require_role(workspace_id, actor, READERS)
        run = await self._store.get_run(workspace_id, run_id, _capabilities(role))
        if run is None:
            raise UnknownRun
        return run

    async def list_runs(
        self, workspace_id: UUID, actor: Actor, session_id: UUID | None
    ) -> Sequence[RunSnapshot]:
        role = await self._require_role(workspace_id, actor, READERS)
        return await self._store.list_runs(workspace_id, session_id, _capabilities(role))

    async def usage_summary(
        self, workspace_id: UUID, actor: Actor
    ) -> WorkspaceUsageSummary:
        """§6's usage half. Gated exactly like `list_runs` on purpose — the
        plan names no separate rule for who may see a workspace's spend, and
        inventing a narrower one here would be a permission this task was not
        asked to design.
        """
        await self._require_role(workspace_id, actor, READERS)
        return await self._store.usage_summary(workspace_id)

    async def open_event_stream(
        self, workspace_id: UUID, actor: Actor, run_id: UUID, after_sequence: int
    ) -> RunEventWindow:
        """Admit a subscriber, or refuse before a single frame is written.

        Refusing here keeps a rejection an ordinary Problem Details response
        instead of a half-open stream a browser would have to interpret.
        """
        window = await self.event_window(workspace_id, actor, run_id)
        if cursor_is_stale(after_sequence, window.earliest_sequence, window.next_sequence):
            raise RunEventCursorTooOld(
                window.earliest_sequence
                if window.earliest_sequence is not None
                else window.next_sequence
            )
        return window

    async def event_window(
        self, workspace_id: UUID, actor: Actor, run_id: UUID
    ) -> RunEventWindow:
        await self._require_role(workspace_id, actor, READERS)
        window = await self._store.event_window(workspace_id, run_id)
        if window is None:
            raise UnknownRun
        return window

    async def read_events(
        self,
        workspace_id: UUID,
        actor: Actor,
        run_id: UUID,
        after_sequence: int,
        limit: int,
    ) -> Sequence[RunEventRecord]:
        await self._require_role(workspace_id, actor, READERS)
        return await self._store.list_events_after(
            workspace_id, run_id, after_sequence, limit
        )

    async def control_run(
        self,
        workspace_id: UUID,
        actor: Actor,
        run_id: UUID,
        signal: RunSignal,
        expected_state_version: int,
        request_id: str,
    ) -> RunSnapshot:
        role = await self._require_role(workspace_id, actor, WRITERS)
        return await self._store.control_run(
            ControlRunCommand(
                workspace_id=workspace_id,
                run_id=run_id,
                caller=_caller(actor),
                capabilities=_capabilities(role),
                signal=signal,
                expected_state_version=expected_state_version,
                request_id=request_id,
            )
        )

    async def widen_budget(
        self,
        workspace_id: UUID,
        actor: Actor,
        run_id: UUID,
        expected_state_version: int,
        max_model_calls: int,
        request_id: str,
    ) -> RunSnapshot:
        role = await self._require_role(workspace_id, actor, BUDGET_HOLDERS)
        return await self._store.widen_budget(
            WidenBudgetCommand(
                workspace_id=workspace_id,
                run_id=run_id,
                caller=_caller(actor),
                capabilities=_capabilities(role),
                expected_state_version=expected_state_version,
                max_model_calls=max_model_calls,
                request_id=request_id,
            )
        )

    async def apply_signal(self, command: ApplySignalCommand) -> RunSnapshot:
        """The seam a future Worker or Scheduler process calls.

        It carries no browser identity because those processes act as the
        platform itself, not as a workspace member.
        """
        return await self._store.apply_signal(command)

    async def claim_head_run(self, command: ClaimRunCommand) -> ClaimedRun | None:
        return await self._store.claim_head(command)

    async def repair_session_head(self, session_id: UUID, request_id: str) -> RepairResult:
        return await self._store.repair_session_head(session_id, request_id)

    async def list_session_messages(
        self, workspace_id: UUID, actor: Actor, session_id: UUID
    ) -> Sequence[CanonicalMessage]:
        await self._load_readable_session(workspace_id, actor, session_id)
        return await self._store.list_session_messages(workspace_id, session_id)

    async def read_session_messages(
        self, workspace_id: UUID, actor: Actor, session_id: UUID, request_id: str
    ) -> Sequence[CanonicalMessage]:
        """§6: the console's read of message content.

        §4.6 opened "查看" for a developer at the price of this audit row —
        written only when the session belongs to an end user, and only here.
        `list_sessions` returns titles and never calls this: a page of forty
        of them must not turn into forty rows that bury the read that
        actually matters.
        """
        session = await self._load_readable_session(workspace_id, actor, session_id)
        if session.caller.caller_type is CallerType.END_USER:
            await self._store.record_end_user_session_read(
                workspace_id,
                _caller(actor),
                session.caller.caller_id,
                session_id,
                request_id,
            )
        return await self._store.list_session_messages(workspace_id, session_id)

    async def _load_readable_session(
        self, workspace_id: UUID, actor: Actor, session_id: UUID
    ) -> SessionSnapshot:
        await self._require_role(workspace_id, actor, READERS)
        session = await self._store.get_session(workspace_id, session_id)
        if session is None:
            raise UnknownSession
        return session

    async def claim_idempotency(
        self,
        workspace_id: UUID,
        actor: Actor,
        endpoint: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> AcceptedRun | None:
        await self._require_role(workspace_id, actor, WRITERS)
        return await self._store.claim_idempotency(
            workspace_id,
            _caller(actor).caller_type,
            _caller(actor).caller_id,
            endpoint,
            idempotency_key,
            fingerprint,
        )

    async def store_idempotency_response(
        self,
        workspace_id: UUID,
        actor: Actor,
        endpoint: str,
        idempotency_key: str,
        run_id: UUID,
        document: dict[str, Any],
    ) -> None:
        await self._require_role(workspace_id, actor, WRITERS)
        caller = _caller(actor)
        await self._store.store_idempotency_response(
            workspace_id,
            caller.caller_type,
            caller.caller_id,
            endpoint,
            idempotency_key,
            run_id,
            document,
        )

    async def _require_role(
        self, workspace_id: UUID, actor: Actor, allowed: set[Role]
    ) -> Role:
        if actor.is_service_account:
            if actor.role is None or actor.role not in allowed:
                raise ForbiddenRunAction
            return actor.role
        role = await self._store.role_for(workspace_id, actor.id)
        if role is not None:
            if role not in allowed:
                raise ForbiddenRunAction
            return role
        if not actor.is_platform_admin:
            raise ForbiddenRunAction
        return Role.WORKSPACE_ADMIN


def _caller(actor: Actor) -> CallerIdentity:
    kind = CallerType.SERVICE_ACCOUNT if actor.is_service_account else CallerType.USER
    return CallerIdentity(kind, actor.id)


def _capabilities(role: Role) -> RunCapabilities:
    writable = role in WRITERS
    return RunCapabilities(can_control=writable, can_retry=writable)


def _require_idempotency_key(value: str | None) -> str:
    normalized = (value or "").strip()
    if not normalized or len(normalized) > 255:
        raise IdempotencyKeyRequired
    return normalized
