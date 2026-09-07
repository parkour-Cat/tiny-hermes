"""Index dialogue text without the model's reasoning or future block metadata.

Both kinds of block have a `text` field. The old JSON path indexed both, so
filtering snippets alone still matched requests that appeared only in reasoning.
Rebuilding the generated column also corrects existing rows without changing
their canonical content, which the model may need for replay.
"""

from alembic import op

revision: str = "20260906_0057"
down_revision: str | None = "20260904_0056"
branch_labels: None = None
depends_on: None = None

VISIBLE_TEXT = (
    "jsonb_path_query_array(content::jsonb, '$.parts[*] ? (@.type == \"text\").text')::text"
)
PREVIOUS_TEXT = "jsonb_path_query_array(content::jsonb, '$.parts[*].text')::text"


def _replace_index(message_text: str) -> None:
    op.execute(
        "ALTER TABLE session_messages DROP COLUMN search, "
        "ADD COLUMN search tsvector GENERATED ALWAYS AS ("
        f"to_tsvector('simple', {message_text}) "
        f"|| to_tsvector('simple', th_cjk_bigrams({message_text}))"
        ") STORED"
    )
    op.create_index(
        "ix_session_messages_search",
        "session_messages",
        ["search"],
        postgresql_using="gin",
    )


def upgrade() -> None:
    _replace_index(VISIBLE_TEXT)


def downgrade() -> None:
    _replace_index(PREVIOUS_TEXT)
