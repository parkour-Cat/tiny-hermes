"""The Runs list, newest first.

Written after a person opened the console, saw their newest Run at the
bottom of the page, and asked why. It is the second time this exact
question has been asked about this product — workspaces had the same
ordering and the same complaint — which is what makes it worth a test
rather than a one-line fix.

A list of things that happened is read newest-first. Oldest-first is right
for a transcript, where the order *is* the meaning; it is wrong for a
worklist, where the thing you came to look at is the thing that just
happened and every new row pushes it further from the top.
"""


from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


def _submit(client: TestClient, scope: dict[str, str], session_id: str, key: str) -> str:
    created = client.post(
        "/api/v1/runs",
        headers={**scope, "Idempotency-Key": key},
        json={"session_id": session_id, "input": f"message {key}"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def test_the_newest_run_is_listed_first(
    client: TestClient, scope: dict[str, str], session_id: str
) -> None:
    first = _submit(client, scope, session_id, "run-a")
    second = _submit(client, scope, session_id, "run-b")
    third = _submit(client, scope, session_id, "run-c")

    listed = client.get("/api/v1/runs", headers=scope)

    assert listed.status_code == 200, listed.text
    ids = [str(entry["id"]) for entry in listed.json()]
    assert ids[:3] == [third, second, first]


async def test_runs_sharing_a_timestamp_still_have_one_order(
    client: TestClient, scope: dict[str, str], session_id: str, engine: AsyncEngine
) -> None:
    """`created_at` is not a total order, so the tiebreaker has to carry it.

    The timestamps are forced equal on purpose. The first version of this
    test just submitted five Runs and listed twice — and it passed with the
    tiebreaker deliberately removed, because real submissions differ by
    microseconds and the collision it claimed to cover never happened. It
    was a test that could not fail for the reason it existed.

    CLAUDE.md names this exact shape: several tables order by `created_at`
    with no tiebreaker. With rows genuinely tied, the database may return
    them in a different order on each request — the symptom is not a wrong
    list, it is one that shuffles itself while you look at it.
    """
    submitted = [
        _submit(client, scope, session_id, key)
        for key in ("run-1", "run-2", "run-3", "run-4", "run-5")
    ]
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET created_at = timestamptz '2026-08-25 00:00:00+00'")
        )

    listed = [str(e["id"]) for e in client.get("/api/v1/runs", headers=scope).json()]

    # Asserted as a *determined* order rather than as "the same twice".
    # Listing twice and comparing was this test's second attempt and it also
    # passed with the tiebreaker removed: on a small table Postgres returns
    # the physical order, which is perfectly stable while nothing else
    # writes. Stability was never the property worth checking.
    #
    # With `created_at` tied, `session_sequence` is what decides — so the
    # newest-first list must be exactly the reverse of the order they were
    # submitted in. That is an assertion no ordering *but* the intended one
    # satisfies. (`id` sits behind it as the last resort for rows that tie
    # on both, which needs two Sessions to reach and is not covered here.)
    assert listed == list(reversed(submitted))


def test_a_session_filtered_listing_stays_in_queue_order(
    client: TestClient, scope: dict[str, str], session_id: str
) -> None:
    """Filtered to one Session, the order flips back — deliberately.

    This assertion was written the other way round first, on the reasoning
    that a filter should not change the order. An existing test caught it:
    filtered to a Session, this list **is** the queue. `queue.position`
    counts 1, 2, 3 down it, and newest-first would put position 1 at the
    bottom of a numbered list.

    The two listings answer different questions. "What has been happening"
    is read from the top. "What is this Session doing, in order" is a
    transcript, where the order carries the meaning.
    """
    first = _submit(client, scope, session_id, "s-a")
    second = _submit(client, scope, session_id, "s-b")

    listed = client.get(
        "/api/v1/runs", headers=scope, params={"session_id": session_id}
    )

    body = listed.json()
    assert [str(entry["id"]) for entry in body][:2] == [first, second]
    assert [entry["queue"]["position"] for entry in body][:2] == [1, 2]
