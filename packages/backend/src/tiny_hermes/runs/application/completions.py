"""Chat Completions: claim first, then a Run, then wait for words.

The compatibility surface must not create an ephemeral Session before it knows
whether this Idempotency-Key already belongs to a finished request. Replay of
a default Completions call would otherwise leak a Session on every retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiny_hermes.agents.application.service import (
    AgentCatalog,
    ForbiddenAgentAction,
    UnknownAgent,
)
from tiny_hermes.agents.domain.models import Agent, AgentSpec, AgentVersion
from tiny_hermes.agents.infrastructure.sql_store import SqlAgentStore
from tiny_hermes.model_catalog.infrastructure.sql_store import SqlModelEndpointStore
from tiny_hermes.runs.application.service import (
    IdempotencyKeyReused,
    RunCoordination,
    UnknownSession,
)
from tiny_hermes.runs.domain.models import RunSnapshot, RunState, SessionMode
from tiny_hermes.runs.infrastructure.sql_store import SqlRunStore
from tiny_hermes.runs.ports.notifier import WakeUpNotifier
from tiny_hermes.tenancy.domain.models import Actor

COMPLETIONS_ENDPOINT = "POST /v1/chat/completions"
BLOCKING_HEAD_STATES = frozenset(
    {RunState.PAUSED, RunState.WAITING_APPROVAL, RunState.WAITING_EXTERNAL}
)
TERMINAL_WAIT_STATES = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
)
POLL_SECONDS = 0.05


class CompletionsError(Exception):
    """An OpenAI-shaped refusal. Auth failures stay Problem Details."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "message": self.message,
            "type": "invalid_request_error",
            "param": None,
            "code": self.code,
        }
        error.update(self.extra)
        return {"error": error}


def fingerprint_completions(
    workspace_id: UUID,
    model: str,
    messages: list[dict[str, Any]],
    session_id: str | None,
    stream: bool,
) -> str:
    payload = {
        "endpoint": COMPLETIONS_ENDPOINT,
        "workspace_id": str(workspace_id),
        "model": model,
        "messages": messages,
        "session_id": session_id,
        "stream": stream,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        raise CompletionsError(
            400,
            "invalid_request",
            "The last user message must have string content.",
        )
    raise CompletionsError(
        400, "invalid_request", "messages must include a user turn."
    )


async def complete_chat(
    *,
    sessions: async_sessionmaker[AsyncSession],
    notifier: WakeUpNotifier,
    actor: Actor,
    workspace_id: UUID,
    model: str,
    messages: list[dict[str, Any]],
    stream: bool,
    idempotency_key: str | None,
    session_header: str | None,
    request_id: str,
) -> dict[str, Any]:
    if stream:
        raise CompletionsError(
            400, "stream_not_supported", "Streaming completions are not enabled yet."
        )
    key = _require_key(idempotency_key)
    bound_session_id = _parse_session_header(session_header)
    text = user_text(messages)
    fingerprint = fingerprint_completions(
        workspace_id,
        model,
        messages,
        None if bound_session_id is None else str(bound_session_id),
        stream,
    )

    async with sessions() as session:
        catalog = AgentCatalog(SqlAgentStore(session), SqlModelEndpointStore(session))
        runs = RunCoordination(SqlRunStore(session))
        agent, _, timeout = await _published_delivery(
            catalog, workspace_id, actor, model, request_id
        )
        session_id = await _existing_or_none(
            runs, workspace_id, actor, agent.id, bound_session_id
        )
        try:
            replay = await runs.claim_idempotency(
                workspace_id, actor, COMPLETIONS_ENDPOINT, key, fingerprint
            )
        except IdempotencyKeyReused as error:
            raise CompletionsError(
                409,
                "idempotency_key_reused",
                "That idempotency key already belongs to a different request.",
            ) from error
        if replay is not None:
            await session.commit()
            return replay.document
        if session_id is None:
            created = await runs.create_session(
                workspace_id, actor, agent.id, SessionMode.EPHEMERAL, request_id
            )
            session_id = created.id
        accepted = await runs.submit_run(
            workspace_id,
            actor,
            session_id,
            text,
            str(uuid4()),
            request_id,
        )
        await session.commit()

    await notifier.publish(workspace_id, accepted.run_id)
    snapshot = await _wait_for_run(
        sessions, workspace_id, actor, accepted.run_id, timeout
    )
    if snapshot.state is not RunState.COMPLETED:
        raise _unfinished(snapshot.state, accepted.run_id)
    document = await _completion_document(
        sessions, workspace_id, actor, model, session_id, accepted.run_id
    )
    async with sessions() as session:
        runs = RunCoordination(SqlRunStore(session))
        await runs.store_idempotency_response(
            workspace_id,
            actor,
            COMPLETIONS_ENDPOINT,
            key,
            accepted.run_id,
            document,
        )
        await session.commit()
    return document


async def _published_delivery(
    catalog: AgentCatalog,
    workspace_id: UUID,
    actor: Actor,
    model: str,
    request_id: str,
) -> tuple[Agent, AgentVersion, int]:
    try:
        agent, version = await catalog.published_alias(
            workspace_id, actor, model, request_id
        )
    except UnknownAgent as error:
        raise CompletionsError(
            404, "model_not_found", "No published agent uses that alias."
        ) from error
    except ForbiddenAgentAction as error:
        raise CompletionsError(
            403, "forbidden", "The current caller cannot use that agent."
        ) from error
    spec = AgentSpec.model_validate(version.spec)
    if not spec.delivery.enabled:
        raise CompletionsError(
            400,
            "agent_not_compatible",
            "That agent has not enabled Chat Completions delivery.",
        )
    return agent, version, spec.delivery.sync_timeout_seconds


async def _existing_or_none(
    runs: RunCoordination,
    workspace_id: UUID,
    actor: Actor,
    agent_id: UUID,
    session_id: UUID | None,
) -> UUID | None:
    if session_id is None:
        return None
    try:
        found = await runs.get_session(workspace_id, actor, session_id)
    except UnknownSession as error:
        raise CompletionsError(
            404, "session_not_found", "No such session exists."
        ) from error
    if (
        found.session_mode is not SessionMode.PERSISTENT
        or found.agent_id != agent_id
        or found.caller.caller_id != actor.id
    ):
        raise CompletionsError(404, "session_not_found", "No such session exists.")
    if found.head_run_id is None:
        return found.id
    head = await runs.get_run(workspace_id, actor, found.head_run_id)
    if head.state in BLOCKING_HEAD_STATES:
        raise CompletionsError(
            409,
            "session_blocked",
            "The persistent session is blocked by its head run.",
            session_id=str(found.id),
            blocked_by_run_id=str(head.id),
            head_status=head.state.value,
            head_reason={
                "pause_reason": (
                    None if head.pause_reason is None else head.pause_reason.value
                ),
                "wait_kind": head.wait_kind,
                "wait_deadline_at": (
                    None
                    if head.wait_deadline_at is None
                    else head.wait_deadline_at.isoformat()
                ),
            },
            available_actions=list(head.available_actions),
            runs_api_url=f"/api/v1/runs/{head.id}",
        )
    return found.id


async def _wait_for_run(
    sessions: async_sessionmaker[AsyncSession],
    workspace_id: UUID,
    actor: Actor,
    run_id: UUID,
    timeout_seconds: int,
) -> RunSnapshot:
    deadline = monotonic() + timeout_seconds
    while True:
        async with sessions() as session:
            runs = RunCoordination(SqlRunStore(session))
            snapshot = await runs.get_run(workspace_id, actor, run_id)
        if snapshot.state in TERMINAL_WAIT_STATES or snapshot.state in BLOCKING_HEAD_STATES:
            return snapshot
        if monotonic() >= deadline:
            raise CompletionsError(
                504,
                "compat_timeout",
                "The sync window elapsed before the run finished.",
                run_id=str(run_id),
            )
        await asyncio.sleep(POLL_SECONDS)


def _unfinished(state: RunState, run_id: UUID) -> CompletionsError:
    if state in BLOCKING_HEAD_STATES:
        return CompletionsError(
            409,
            "requires_runs_api",
            "The run left the compatibility surface.",
            run_id=str(run_id),
        )
    return CompletionsError(
        500, "run_failed", "The run did not complete.", run_id=str(run_id)
    )


async def _completion_document(
    sessions: async_sessionmaker[AsyncSession],
    workspace_id: UUID,
    actor: Actor,
    model: str,
    session_id: UUID,
    run_id: UUID,
) -> dict[str, Any]:
    async with sessions() as session:
        runs = RunCoordination(SqlRunStore(session))
        messages = await runs.list_session_messages(workspace_id, actor, session_id)
    content = ""
    for message in reversed(messages):
        if message.role == "assistant" and message.text:
            content = message.text
            break
    return {
        "id": f"chatcmpl-{run_id}",
        "object": "chat.completion",
        "created": int(datetime.now(UTC).timestamp()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _require_key(value: str | None) -> str:
    normalized = (value or "").strip()
    if not normalized or len(normalized) > 255:
        raise CompletionsError(
            400,
            "idempotency_key_required",
            "A non-empty Idempotency-Key header is required.",
        )
    return normalized


def _parse_session_header(raw: str | None) -> UUID | None:
    if raw is None or not raw.strip():
        return None
    try:
        return UUID(raw.strip())
    except ValueError as error:
        raise CompletionsError(
            404, "session_not_found", "No such session exists."
        ) from error
