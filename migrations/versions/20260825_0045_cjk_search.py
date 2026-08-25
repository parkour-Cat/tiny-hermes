"""Make full-text search work for Chinese.

`to_tsvector('simple', …)` splits on whitespace and punctuation. English
comes out as words; **Chinese comes out as one token per sentence**, so a
query matched only when it was character-for-character identical to the
stored text. Every realistic Chinese search returned nothing.

Two places depended on it and both were broken:

- §14.3's `session.search`, the way a Run looks back at what was said
  before — the model's only route to anything compaction has summarized
  away.
- `memories.search`, which orders a subject's remembered facts by relevance
  to the current request. With no relevance computable, the order was
  arbitrary — and that order decides **which memories survive** when the
  segment is over budget.

`simple` is kept rather than replaced. The reasoning recorded on the
original column is correct: a stemmer for one language mangles the other,
and there is no built-in configuration that handles both. What is added is
a second index over character bigrams, ORed with the first at query time:
English keeps matching as words, Chinese matches as overlapping pairs.

Bigrams rather than single characters: single-character tokens match almost
everything, and a search that returns every session is as useless as one
that returns none and harder to notice. The cost is that a one-character
query cannot match — worth stating, since it silently returns nothing.

`th_cjk_bigrams` is IMMUTABLE because a generated column requires it, and
it genuinely is: same text in, same pairs out, no dependency on collation
or locale. The CJK range covers Han ideographs only; kana and Hangul are
segmented tolerably by `simple` already and are not in scope here.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0045"
down_revision: str | None = "20260825_0044"
branch_labels: None = None
depends_on: None = None

BIGRAMS = """
CREATE OR REPLACE FUNCTION th_cjk_bigrams(t text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$
  SELECT coalesce(string_agg(substring(t, i, 2), ' '), '')
  FROM generate_series(1, length(t) - 1) AS i
  WHERE substring(t, i, 1) ~ '[㐀-鿿]'
    AND substring(t, i + 1, 1) ~ '[㐀-鿿]'
$$;
"""

#: The message text, as `session_messages.search` already extracted it.
MESSAGE_TEXT = "jsonb_path_query_array(content::jsonb, '$.parts[*].text')::text"


def upgrade() -> None:
    op.execute(BIGRAMS)
    op.execute(
        "ALTER TABLE session_messages DROP COLUMN search, "
        "ADD COLUMN search tsvector GENERATED ALWAYS AS ("
        f"  to_tsvector('simple', {MESSAGE_TEXT})"
        f"  || to_tsvector('simple', th_cjk_bigrams({MESSAGE_TEXT}))"
        ") STORED"
    )
    op.create_index(
        "ix_session_messages_search",
        "session_messages",
        ["search"],
        postgresql_using="gin",
    )
    op.execute(
        "ALTER TABLE memories DROP COLUMN search, "
        "ADD COLUMN search tsvector GENERATED ALWAYS AS ("
        "  to_tsvector('simple', body)"
        "  || to_tsvector('simple', th_cjk_bigrams(body))"
        ") STORED"
    )
    op.create_index(
        "ix_memories_search", "memories", ["search"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_memories_search", table_name="memories")
    op.execute(
        "ALTER TABLE memories DROP COLUMN search, "
        "ADD COLUMN search tsvector GENERATED ALWAYS AS "
        "(to_tsvector('simple', body)) STORED"
    )
    op.create_index(
        "ix_memories_search", "memories", ["search"], postgresql_using="gin"
    )
    op.drop_index("ix_session_messages_search", table_name="session_messages")
    op.execute(
        "ALTER TABLE session_messages DROP COLUMN search, "
        "ADD COLUMN search tsvector GENERATED ALWAYS AS "
        f"(to_tsvector('simple', {MESSAGE_TEXT})) STORED"
    )
    op.create_index(
        "ix_session_messages_search",
        "session_messages",
        ["search"],
        postgresql_using="gin",
    )
    op.execute("DROP FUNCTION IF EXISTS th_cjk_bigrams(text)")


sa  # noqa: B018 - alembic templates import it; kept for consistency
