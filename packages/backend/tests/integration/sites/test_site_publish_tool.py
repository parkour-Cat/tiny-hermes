"""An Agent publishes its site and hands the link back (§1.26, v2.26)."""

from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tiny_hermes.runs.application.worker import WorkerRuntime, WorkerSettings
from tiny_hermes.runs.domain.models import ToolCallBlock
from tiny_hermes.runs.infrastructure.null_notifier import NullWakeUpNotifier
from tiny_hermes.runs.ports.model import ModelRequest, ModelResponse, StopReason
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.shared.config import Settings
from tiny_hermes.sites.application.publisher import SitePublisher, SiteSettings
from tiny_hermes.sites.infrastructure.tables import SitePublicationRow

from ..conftest import VALID_SPEC
from ..workspace_file_support import seed_site
from .conftest import SITE_PUBLIC_URL, SITE_SECRET


class PublishThenDone:
    def __init__(self, path: str = "site") -> None:
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
                    ToolCallBlock(
                        call_id="p1",
                        name="site.publish",
                        arguments={"path": self._path, "title": "Ledger"},
                    ),
                ),
            )
        return ModelResponse(stop_reason=StopReason.COMPLETED, text="done")


def _run(client: TestClient, scope: dict[str, str]) -> str:
    agent = client.post(
        "/api/v1/agents", headers=scope, json={"name": "Site", "alias": "site-maker"}
    ).json()
    draft = client.put(
        f"/api/v1/agents/{agent['id']}/draft",
        headers=scope,
        json={"expected_revision": 1, "spec": {**VALID_SPEC, "tools": ["site.publish"]}},
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
        headers={**scope, "Idempotency-Key": "site-run"},
        json={"session_id": session, "input": "make me a site"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _slice(engine: AsyncEngine, settings: Settings, model: Any, public: str | None) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await WorkerRuntime(
        session_factory=sessions,
        model=model,
        notifier=NullWakeUpNotifier(),
        sites=SitePublisher(
            sessions=sessions,
            objects=MinioObjectStore(
                endpoint=settings.s3_endpoint,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                bucket=settings.s3_bucket,
            ),
            settings=SiteSettings(public, SITE_SECRET, 7),
        ),
        settings=WorkerSettings(
            worker_id="worker-sites", lease_seconds=30, max_slice_seconds=30, idle_poll_seconds=1
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
        return "\n".join(str(row.c) for row in rows.all())


async def _status(engine: AsyncEngine, run_id: str) -> str:
    async with engine.connect() as db:
        return str(await db.scalar(text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}))


async def test_an_agent_publishes_its_site_and_is_handed_the_link(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, settings: Settings
) -> None:
    run_id = _run(client, scope)
    await seed_site(engine, settings, run_id, {"site/index.html": b"<h1>Ledger</h1>"})

    await _slice(engine, settings, PublishThenDone(), SITE_PUBLIC_URL)

    assert await _status(engine, run_id) == "completed"
    transcript = await _transcript(engine, run_id)
    assert f"{SITE_PUBLIC_URL}/s/" in transcript
    async with engine.connect() as db:
        row = (
            await db.execute(
                select(
                    SitePublicationRow.run_id,
                    SitePublicationRow.title,
                    SitePublicationRow.visibility,
                )
            )
        ).one()
    assert row.run_id == UUID(run_id) and row.title == "Ledger" and row.visibility == "members"


async def test_a_directory_that_is_not_a_site_is_refused_back_to_the_model(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, settings: Settings
) -> None:
    run_id = _run(client, scope)
    await seed_site(engine, settings, run_id, {"site/home.html": b"<h1>no index</h1>"})

    await _slice(engine, settings, PublishThenDone(), SITE_PUBLIC_URL)

    transcript = await _transcript(engine, run_id)
    assert "no_index" in transcript
    async with engine.connect() as db:
        assert await db.scalar(select(func.count()).select_from(SitePublicationRow)) == 0


async def test_a_deployment_without_a_site_origin_says_so(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, settings: Settings
) -> None:
    run_id = _run(client, scope)
    await seed_site(engine, settings, run_id, {"site/index.html": b"<h1>x</h1>"})

    await _slice(engine, settings, PublishThenDone(), None)

    assert "does not publish sites" in await _transcript(engine, run_id)
