"""Who the subject of a data-rights request actually is.

`subject_routes.py` built every subject as `CallerType.USER` — a decision
its own docstring defended by pointing out that the router is console-only
and an end user never *calls* it. True, and beside the point: the caller
being a console user says nothing about whose data they are acting on, and
§4.6's row is specifically about an administrator acting for somebody else.

The consequence was as bad as it gets for this feature. An administrator
erasing an end user got `200` and `{"memories":0,...}` while every row
stayed exactly where it was, because the deletes matched on
`caller_type='user'` and the subject's rows say `'end_user'`. An erasure
that reports success and erases nothing is worse than one that fails.
"""

from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def _end_user_with_a_memory(
    engine: AsyncEngine, workspace_id: str
) -> tuple[UUID, UUID]:
    subject = uuid4()
    agent = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO end_users (id, workspace_id, created_at) "
                "VALUES (:i, :w, now())"
            ),
            {"i": subject, "w": UUID(workspace_id)},
        )
        await connection.execute(
            text(
                "INSERT INTO agents (id, workspace_id, name, alias, status, created_at)"
                " VALUES (:a, :w, 'Helper', 'helper', 'draft', now())"
            ),
            {"a": agent, "w": UUID(workspace_id)},
        )
        await connection.execute(
            text(
                "INSERT INTO memories (id, workspace_id, agent_id, kind, status, body,"
                " origin, subject_type, subject_id, created_at, updated_at)"
                " VALUES (gen_random_uuid(), :w, :a, 'private', 'active',"
                " 'they prefer mornings', 'agent_proposal', 'end_user', :s, now(), now())"
            ),
            {"w": UUID(workspace_id), "a": agent, "s": subject},
        )
    return subject, agent


async def _memories_left(engine: AsyncEngine, subject: UUID) -> int:
    async with engine.connect() as connection:
        found = await connection.execute(
            text("SELECT count(*) FROM memories WHERE subject_id = :s"), {"s": subject}
        )
        return int(found.scalar() or 0)


async def test_erasing_an_end_user_removes_their_memories(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, workspace_id: str
) -> None:
    subject, _ = await _end_user_with_a_memory(engine, workspace_id)
    assert await _memories_left(engine, subject) == 1

    erased = client.post(f"/api/v1/subjects/{subject}/erase", headers=scope)

    assert erased.status_code == 200, erased.text
    assert erased.json()["memories"] == 1
    assert await _memories_left(engine, subject) == 0


async def test_erasing_an_end_user_marks_them_erased(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, workspace_id: str
) -> None:
    """§344: `erased_at` is what stops the same credential resurrecting them.

    Without it, a subject who asked to be forgotten walks back in through
    `EndUserIdentityService.exchange` and is handed their old id.
    """
    subject, _ = await _end_user_with_a_memory(engine, workspace_id)

    client.post(f"/api/v1/subjects/{subject}/erase", headers=scope)

    async with engine.connect() as connection:
        marked = await connection.execute(
            text("SELECT erased_at FROM end_users WHERE id = :s"), {"s": subject}
        )
    assert marked.scalar() is not None


async def test_exporting_an_end_user_returns_what_is_held_about_them(
    client: TestClient, scope: dict[str, str], engine: AsyncEngine, workspace_id: str
) -> None:
    """An empty export is a statement — "nothing is held about you" — and it
    was the answer an administrator got for every end user."""
    subject, _ = await _end_user_with_a_memory(engine, workspace_id)

    exported = client.get(f"/api/v1/subjects/{subject}/export", headers=scope)

    assert exported.status_code == 200, exported.text
    assert exported.json()["subject_type"] == "end_user"
    assert [item["body"] for item in exported.json()["memories"]] == [
        "they prefer mornings"
    ]


async def test_a_subject_id_belonging_to_nobody_is_refused(
    client: TestClient, scope: dict[str, str]
) -> None:
    """Not a cheerful report of zeros.

    "Erased nothing because there was nothing" and "erased nothing because
    that id is not a subject" are different, and an administrator acting on
    a request needs to know which one happened before they answer the
    person who asked.
    """
    refused = client.post(f"/api/v1/subjects/{uuid4()}/erase", headers=scope)

    assert refused.status_code == 404, refused.text
