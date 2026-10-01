"""Indexes for the queries that run against growing history.

An audit of one Scheduler cycle on 2026-09-30 found three queries reading a
whole history table every second, with no index to do otherwise:
- expired idempotency records, by `expires_at`;
- run events past retention, by `occurred_at` — the largest table there is;
- a Run's own messages, by `source_run_id`: the reply dispatch reads them for
  every pending reply, and every Run snapshot (each read through the API, each
  recorded slice) for its first user message.
A table this small at test time cannot show the planner preferring the index;
that was measured at scale (docs/superpowers/verification/).
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.parametrize(
    ("table", "name", "must_mention"),
    [
        ("idempotency_records", "ix_idempotency_records_expiry", ("expires_at", "IS NOT NULL")),
        ("run_events", "ix_run_events_occurred_at", ("occurred_at",)),
        ("session_messages", "ix_session_messages_source_run", ("source_run_id", "sequence")),
    ],
)
async def test_a_query_over_history_has_an_index_to_read_instead(
    engine: AsyncEngine, table: str, name: str, must_mention: tuple[str, ...]
) -> None:
    async with engine.connect() as connection:
        definition = (
            await connection.execute(
                text("SELECT indexdef FROM pg_indexes WHERE tablename = :t AND indexname = :n"),
                {"t": table, "n": name},
            )
        ).scalar_one_or_none()

    assert definition is not None, f"{table} has no {name}"
    for word in must_mention:
        assert word in definition
