"""An Agent sends a saved workspace file back into the Feishu conversation
(§1.27, v2.27): through a real Worker, a real snapshot, a real binding and
its sealed secret; only the network is recorded instead of reached."""

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.channels.infrastructure.file_delivery import ChannelFileSender
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse, StopReason
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.shared.config import Settings

from ..conftest import VALID_SPEC
from ..workspace_file_support import seed_site
from .test_image_source import KEK, _stored_secret  # pyright: ignore[reportPrivateUsage]

REPORT = b"PK\x03\x04 a weekly report"


class SendThenDone:
    def __init__(self, path: str = "out/周报.xlsx") -> None:
        self._path = path
        self.rounds = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.rounds += 1
        if self.rounds == 1:
            return ModelResponse(
                stop_reason=StopReason.TOOL_CALL,
                text="",
                tool_calls=(
                    ToolCallBlock(call_id="f1", name="file.send", arguments={"path": self._path}),
                ),
            )
        return ModelResponse(stop_reason=StopReason.COMPLETED, text="done")


class Recorded:
    """Feishu, recorded: a token, an upload key, a sent message."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        headers: Any = None,
    ) -> httpx.Response:
        self.calls.append({"method": method, "url": url, "json": json, "content": content})
        del headers
        if "tenant_access_token" in url:
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t-abc", "expire": 7200}
            )
        if url.endswith("/im/v1/files"):
            return httpx.Response(200, json={"code": 0, "data": {"file_key": "fk_up"}})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_out"}})

    async def post(self, url: str, *, json: Any = None, headers: Any = None) -> httpx.Response:
        return await self.request("POST", url, json=json, headers=headers)


def _run(client: TestClient, scope: dict[str, str]) -> str:
    agent = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Office", "alias": "office"}
    ).json()
    draft = client.put(
        f"/api/v1/agents/{agent['id']}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": {**VALID_SPEC, "tools": ["file.send"]}},
    )
    assert draft.status_code == 200, draft.text
    client.post(
        f"/api/v1/agents/{agent['id']}/publish",
        headers=scope,
        json={"expected_revision": draft.json()["revision"]},
    ).raise_for_status()
    session = client.post(
        "/api/v1/sessions", headers=scope, json={"agent_id": agent["id"]}
    ).json()["id"]
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": "send-run"},
        json={"session_id": session, "input": "send me the report"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _bind_to_feishu(engine: AsyncEngine, run_id: str) -> None:
    """The run's Session, as if it had arrived from a Feishu chat."""
    async with engine.connect() as db:
        run = (
            await db.execute(
                text(
                    "SELECT r.workspace_id, r.session_id, s.agent_id FROM runs r "
                    "JOIN sessions s ON s.id = r.session_id WHERE r.id = :id"
                ),
                {"id": run_id},
            )
        ).one()
    secret = await _stored_secret(engine, str(run.workspace_id), "s3cret")
    async with engine.begin() as db:
        binding = (
            await db.execute(
                text(
                    "INSERT INTO channel_bindings (id, workspace_id, channel, agent_id,"
                    " status, created_by, created_at, encrypt_key_ref, app_id,"
                    " app_secret_ref) VALUES (gen_random_uuid(), :w, 'feishu', :a, 'active',"
                    " (SELECT id FROM users LIMIT 1), now(), 'K', 'cli_x', :sec) RETURNING id"
                ),
                {"w": run.workspace_id, "a": run.agent_id, "sec": str(secret)},
            )
        ).scalar_one()
        await db.execute(
            text(
                "INSERT INTO channel_conversations (id, channel_binding_id,"
                " external_user_id, session_id, created_at)"
                " VALUES (gen_random_uuid(), :b, 'ou_zhang', :s, now())"
            ),
            {"b": binding, "s": run.session_id},
        )


async def _slice(
    engine: AsyncEngine, settings: Settings, feishu: Recorded, *, max_bytes: int | None = None
) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    objects = MinioObjectStore(
        endpoint=settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        bucket=settings.s3_bucket,
    )
    sender = (
        ChannelFileSender(sessions, objects, lambda _: feishu, KEK)
        if max_bytes is None
        else ChannelFileSender(sessions, objects, lambda _: feishu, KEK, max_bytes=max_bytes)
    )
    await WorkerRuntime(
        session_factory=sessions,
        model=SendThenDone(),
        notifier=NullWakeUpNotifier(),
        file_sends=sender,
        settings=WorkerSettings(
            worker_id="worker-send", lease_seconds=30, max_slice_seconds=30, idle_poll_seconds=1
        ),
    ).run_once()


async def _transcript(engine: AsyncEngine, run_id: str) -> str:
    async with engine.connect() as db:
        rows = await db.execute(
            text(
                "SELECT m.content::text AS c FROM session_messages m "
                "JOIN runs r ON r.session_id = m.session_id WHERE r.id = :run ORDER BY m.sequence"
            ),
            {"run": run_id},
        )
        # Decoded, so the names read as the model read them, not as escapes.
        return "\n".join(
            json.dumps(json.loads(str(row.c)), ensure_ascii=False) for row in rows.all()
        )


async def _one(engine: AsyncEngine, sql: str, run_id: str) -> Any:
    async with engine.connect() as db:
        return (await db.execute(text(sql), {"run": run_id})).one_or_none()


async def test_a_saved_file_is_uploaded_and_sent_to_the_person_who_asked(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, settings: Settings
) -> None:
    run_id = _run(client, scope)
    await seed_site(engine, settings, run_id, {"out/周报.xlsx": REPORT})
    await _bind_to_feishu(engine, run_id)
    feishu = Recorded()

    await _slice(engine, settings, feishu)

    upload = next(call for call in feishu.calls if call["url"].endswith("/im/v1/files"))
    assert upload["content"] is not None and REPORT in upload["content"]
    assert "周报.xlsx".encode() in upload["content"]
    message = next(call for call in feishu.calls if "/im/v1/messages" in call["url"])
    assert message["json"]["receive_id"] == "ou_zhang"
    assert message["json"]["msg_type"] == "file"
    assert json.loads(message["json"]["content"]) == {"file_key": "fk_up"}
    event = await _one(
        engine,
        "SELECT payload FROM run_events "
        "WHERE run_id = CAST(:run AS uuid) AND event_type = 'file_sent'",
        run_id,
    )
    assert event is not None
    assert event.payload == {"path": "out/周报.xlsx", "size": len(REPORT), "delivered": True}
    audit = await _one(
        engine,
        "SELECT context FROM audit_events WHERE action = 'file.sent' "
        "AND context->>'run_id' = :run",
        run_id,
    )
    assert audit is not None
    assert audit.context == {"run_id": run_id, "path": "out/周报.xlsx", "size": str(len(REPORT))}
    assert "Sent" in await _transcript(engine, run_id)


async def test_outside_feishu_nothing_is_sent_and_the_model_is_told_where_it_is(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, settings: Settings
) -> None:
    run_id = _run(client, scope)
    await seed_site(engine, settings, run_id, {"out/周报.xlsx": REPORT})
    feishu = Recorded()

    await _slice(engine, settings, feishu)

    assert feishu.calls == []
    assert "not in Feishu" in await _transcript(engine, run_id)


async def test_a_file_not_yet_saved_is_refused_with_the_reason_to_wait_a_round(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, settings: Settings
) -> None:
    run_id = _run(client, scope)
    await seed_site(engine, settings, run_id, {"out/other.xlsx": REPORT})
    await _bind_to_feishu(engine, run_id)
    feishu = Recorded()

    await _slice(engine, settings, feishu)

    assert not any("/im/v1/" in call["url"] for call in feishu.calls)
    transcript = await _transcript(engine, run_id)
    assert "not_found" in transcript and "own round" in transcript


async def test_a_file_over_the_ceiling_is_refused_not_sent(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, settings: Settings
) -> None:
    run_id = _run(client, scope)
    await seed_site(engine, settings, run_id, {"out/周报.xlsx": REPORT})
    await _bind_to_feishu(engine, run_id)
    feishu = Recorded()

    await _slice(engine, settings, feishu, max_bytes=len(REPORT) - 1)

    assert not any("/im/v1/" in call["url"] for call in feishu.calls)
    assert "too_large" in await _transcript(engine, run_id)
