from tiny_hermes.identity.domain.models import AuthenticatedUser
from tiny_hermes.identity.presentation.dependencies import forbidden
from tiny_hermes.runs.application.service import (
    AgentNotPublished,
    DeniedRunControl,
    EventSequenceConflict,
    ForbiddenRunAction,
    IdempotencyKeyRequired,
    IdempotencyKeyReused,
    RetryBudgetExhausted,
    RetryContextStale,
    RetryLimitReached,
    RetryNotSafe,
    RunCoordinationError,
    SessionAgentNotFound,
    StateVersionConflict,
    UnknownRun,
    UnknownSession,
)
from tiny_hermes.shared.errors import AppError
from tiny_hermes.tenancy.domain.models import Actor


def actor_of(user: AuthenticatedUser) -> Actor:
    return Actor(user.id, user.is_platform_admin)


def as_app_error(error: RunCoordinationError) -> AppError:
    """Turn Run Coordination refusals into Problem Details without leaking content."""
    if isinstance(error, ForbiddenRunAction):
        return forbidden()
    if isinstance(error, SessionAgentNotFound):
        return not_found("agent_not_found", "Agent not found", "agent")
    if isinstance(error, UnknownSession):
        return not_found("session_not_found", "Session not found", "session")
    if isinstance(error, UnknownRun):
        return not_found("run_not_found", "Run not found", "run")
    if isinstance(error, AgentNotPublished):
        return AppError(
            code="agent_not_published",
            title="Agent not published",
            status=409,
            detail="The agent has no published version to run.",
        )
    if isinstance(error, IdempotencyKeyRequired):
        return AppError(
            code="idempotency_key_required",
            title="Idempotency key required",
            status=400,
            detail="A non-empty Idempotency-Key header is required.",
        )
    if isinstance(error, IdempotencyKeyReused):
        return AppError(
            code="idempotency_key_reused",
            title="Idempotency key reused",
            status=409,
            detail="That idempotency key already belongs to a different request.",
        )
    if isinstance(error, StateVersionConflict):
        return AppError(
            code="state_version_conflict",
            title="Run state version conflict",
            status=409,
            detail="The run changed after it was read.",
        )
    if isinstance(error, DeniedRunControl):
        return AppError(
            code=error.code,
            title="Invalid run control",
            status=409,
            detail="The run cannot accept that control in its current state.",
            audited=True,
        )
    if isinstance(error, EventSequenceConflict):
        return AppError(
            code="event_sequence_conflict",
            title="Run event sequence conflict",
            status=409,
            detail="Concurrent writers could not agree on an event sequence.",
        )
    return _retry_error(error)


def not_found(code: str, title: str, noun: str) -> AppError:
    return AppError(
        code=code,
        title=title,
        status=404,
        detail=f"No such {noun} exists in the selected workspace.",
    )


def _retry_error(error: RunCoordinationError) -> AppError:
    codes: list[tuple[type[RunCoordinationError], str, str]] = [
        (RetryNotSafe, "retry_not_safe", "The last checkpoint is not safe to replay."),
        (
            RetryContextStale,
            "retry_context_stale",
            "The session moved on after the source run failed.",
        ),
        (
            RetryBudgetExhausted,
            "retry_budget_exhausted",
            "The shared run budget has no remaining capacity.",
        ),
        (
            RetryLimitReached,
            "retry_limit_reached",
            "The shared retry limit is already used up.",
        ),
    ]
    for kind, code, detail in codes:
        if isinstance(error, kind):
            return AppError(
                code=code,
                title=code.replace("_", " ").capitalize(),
                status=409,
                detail=detail,
            )
    return AppError(
        code="run_request_rejected",
        title="Run request rejected",
        status=422,
        detail="The run request could not be completed.",
    )
