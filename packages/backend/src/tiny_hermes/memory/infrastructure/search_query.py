"""One place that turns a person's words into a full-text query.

Shared rather than repeated at each call site, because there are four of
them and the failure of getting one wrong is silence: a search that quietly
matches nothing looks exactly like a search with no results.

Why this exists at all: `to_tsvector('simple', …)` splits on whitespace and
punctuation, so English becomes words and **Chinese becomes one token per
sentence**. A Chinese query matched only when it was character-for-character
identical to the stored text. Migration 0045 adds a bigram index alongside
the word index; this builds the matching query.
"""

from typing import Any

from sqlalchemy import ColumnElement, func

#: `simple` for the reason the original columns chose it: a stemmer for one
#: language mangles the other, and no built-in configuration handles both.
SEARCH_CONFIG = "simple"

#: Han ideographs. The same range migration 0045's `th_cjk_bigrams` uses.
#:
#: Two definitions of "is this CJK" therefore exist, and they are allowed to
#: drift **in one direction only**. This one decides whether to add the
#: bigram branch at all; the SQL one decides which characters get paired. If
#: this were ever wrong the query degrades to word matching — the behaviour
#: before 0045 — which is a worse search, never a wrong one.
_HAN_START, _HAN_END = "㐀", "鿿"


def _has_han(text: str) -> bool:
    return any(_HAN_START <= character <= _HAN_END for character in text)


def matching(query: str) -> ColumnElement[Any]:
    """A `tsquery` that finds this query in a column built by migration 0045.

    English keeps matching as words. Chinese matches as overlapping
    character pairs, ORed with the word form so a mixed sentence finds
    either half.

    The bigram branch is added only when the query actually contains Han
    characters. It is not merely an optimisation: `th_cjk_bigrams` returns
    an empty string for pure ASCII, and `plainto_tsquery` on empty text
    raises a NOTICE on every single call — a log line per search, for a
    branch that could never match anything.
    """
    words = func.plainto_tsquery(SEARCH_CONFIG, query)
    if not _has_han(query):
        return words
    pairs = func.plainto_tsquery(SEARCH_CONFIG, func.th_cjk_bigrams(query))
    # `||` is OR for tsquery. AND would demand a document contain both the
    # whole phrase as one token *and* its pairs, which no document does.
    return words.op("||")(pairs)


__all__ = ["SEARCH_CONFIG", "matching"]
