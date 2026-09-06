"""Verify old/new search projections across migrations in the isolated test database."""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tiny_hermes.runs.domain.models import CanonicalMessage, ReasoningBlock, TextBlock

state_file = Path(".superpowers/visible-search-migration-state.json")


async def main():
    url = os.environ["DATABASE_URL"]
    if not url.endswith("/tiny_hermes_test"):
        raise RuntimeError("This verifier may only use the isolated tiny_hermes_test database")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as db:
            if sys.argv[1] == "seed":
                row = (
                    await db.execute(text("SELECT id, workspace_id FROM sessions LIMIT 1"))
                ).one()
                message = CanonicalMessage(
                    role="assistant",
                    blocks=(
                        ReasoningBlock(text="migrationhiddenquartz 索引推演"),
                        TextBlock(text="migrationpelican 迁移测试确认"),
                    ),
                )
                state = {"id": str(uuid4()), "content": message.document()}
                await db.execute(
                    text(
                        "INSERT INTO session_messages (id, session_id, workspace_id, sequence, role, content, redacted, created_at) VALUES (:i,:s,:w,(SELECT coalesce(max(sequence),0)+1 FROM session_messages WHERE session_id=:s),'assistant',:c,false,now())"
                    ),
                    {
                        "i": UUID(state["id"]),
                        "s": row.id,
                        "w": row.workspace_id,
                        "c": json.dumps(state["content"]),
                    },
                )
                state_file.write_text(json.dumps(state), encoding="utf-8")
                print("seeded an existing message under the previous schema")
                return
            state = json.loads(state_file.read_text(encoding="utf-8"))
            content = (
                await db.execute(
                    text("SELECT content FROM session_messages WHERE id=:i"),
                    {"i": UUID(state["id"])},
                )
            ).scalar_one()
            assert content == state["content"], "canonical message changed"
            for query, visible in [
                ("migrationhiddenquartz", False),
                ("索引推演", False),
                ("migrationpelican", True),
                ("迁移测试", True),
            ]:
                found = (
                    await db.execute(
                        text(
                            "SELECT search @@ (plainto_tsquery('simple', :q) || plainto_tsquery('simple', th_cjk_bigrams(:q))) FROM session_messages WHERE id=:i"
                        ),
                        {"i": UUID(state["id"]), "q": query},
                    )
                ).scalar_one()
                expected = visible or sys.argv[1] == "old"
                assert found == expected, (query, found, expected)
            indexes = (
                await db.execute(
                    text(
                        "SELECT count(*) FROM pg_indexes WHERE tablename='session_messages' AND indexname='ix_session_messages_search' AND indexdef LIKE '%USING gin%'"
                    )
                )
            ).scalar_one()
            assert indexes == 1
            print(
                sys.argv[1] + ": message unchanged; English/Chinese matches and GIN index verified"
            )
    finally:
        await engine.dispose()


asyncio.run(main())
