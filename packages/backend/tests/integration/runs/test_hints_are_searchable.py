"""The hint a compaction leaves behind must actually find the text it came from.

This is the join the whole feature rests on, and it is exactly the shape
this repository keeps getting wrong: the hints are computed correctly, put
into the summary correctly, and would be **useless** if the search index
tokenized text differently from the way they were extracted.

What this does **not** prove, despite an earlier version of this docstring
saying so: that the extraction granularity matches the index's. Changing
the extractor to three-character grams leaves both tests passing, because
`matching()` runs the query itself through `th_cjk_bigrams` — a hint of any
granularity gets re-split on the way in.

That is a real robustness property rather than a gap, and it is worth
naming: the hints do not have to agree with the index, only to be text
drawn from the conversation. What the tests do have teeth for is the thing
that matters — replacing the hints with an unrelated term fails both.
"""

from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tiny_hermes.runs.domain.context_budget import compaction_hints
from tiny_hermes.runs.domain.models import CanonicalMessage, StoredMessage, TextBlock


def _said(sequence: int, body: str) -> StoredMessage:
    return StoredMessage(
        id=uuid4(),
        sequence=sequence,
        message=CanonicalMessage(role="user", blocks=(TextBlock(text=body),)),  # pyright: ignore[reportArgumentType]
    )


async def _store(engine: AsyncEngine, workspace_id: str, body: str) -> UUID:
    session_id = uuid4()
    subject = uuid4()
    async with engine.begin() as connection:
        agent = (
            await connection.execute(text("SELECT id FROM agents LIMIT 1"))
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO end_users (id, workspace_id, created_at)"
                " VALUES (:i, :w, now())"
            ),
            {"i": subject, "w": UUID(workspace_id)},
        )
        await connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, agent_id, session_mode,"
                " caller_type, caller_id, next_run_sequence, next_message_sequence,"
                " created_at) VALUES (:i, :w, :a, 'persistent', 'end_user', :c, 1, 1,"
                " now())"
            ),
            {"i": session_id, "w": UUID(workspace_id), "a": agent, "c": subject},
        )
        await connection.execute(
            text(
                "INSERT INTO session_messages (id, session_id, workspace_id, sequence,"
                " role, content, redacted, created_at)"
                " VALUES (gen_random_uuid(), :s, :w, 1, 'user', :c, false, now())"
            ),
            {
                "s": session_id,
                "w": UUID(workspace_id),
                "c": '{"parts": [{"type": "text", "text": "' + body + '"}]}',
            },
        )
    return session_id


async def test_a_chinese_hint_finds_the_text_it_was_extracted_from(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    """The end of the chain: compaction removes the text, the hint survives,
    and the hint is enough to get it back."""
    del published_agent
    body = "鹈鹕项目的发布定在下周二"
    await _store(engine, workspace_id, body)
    hints = compaction_hints([_said(1, body), _said(2, "鹈鹕项目还需要新的看板")])
    assert hints, "nothing was extracted, so there is nothing to search for"

    async with engine.connect() as connection:
        found = await connection.execute(
            text(
                "SELECT count(*) FROM session_messages"
                # Parenthesised: `@@` binds tighter than `||`, so without
                # them Postgres reads this as `(search @@ a) || b` and
                # complains about `boolean || tsquery`.
                " WHERE search @@ (plainto_tsquery('simple', :q)"
                "    || plainto_tsquery('simple', th_cjk_bigrams(:q)))"
            ),
            {"q": hints[0]},
        )

    assert found.scalar_one() >= 1


async def test_an_english_hint_finds_its_text_too(
    engine: AsyncEngine, workspace_id: str, published_agent: str
) -> None:
    del published_agent
    body = "the pelican rollout is on Tuesday"
    await _store(engine, workspace_id, body)
    hints = compaction_hints(
        [_said(1, body), _said(2, "pelican needs the new dashboard")]
    )
    assert hints

    async with engine.connect() as connection:
        found = await connection.execute(
            text(
                "SELECT count(*) FROM session_messages"
                " WHERE search @@ plainto_tsquery('simple', :q)"
            ),
            {"q": hints[0]},
        )

    assert found.scalar_one() >= 1
