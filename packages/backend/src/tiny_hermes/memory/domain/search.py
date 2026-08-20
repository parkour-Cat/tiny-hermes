"""What a session search may ask for, and what it is allowed to give back.

Product design §14.3: past sessions are retrieved **on demand** rather than
loaded into the context whole. So the shape of a result matters as much as the
matching — a search that returned whole conversations would be the thing §14.3
exists to prevent, wearing a different name.

Three bounds, and all three refuse rather than truncate quietly:

- **A query has to say something.** An empty query is not "everything"; it is a
  question nobody asked, and answering it with the whole history is how a
  search becomes a dump.
- **A page is small.** The model is choosing what to look at next, not reading
  an archive, and a hundred results is a hundred it did not read.
- **A snippet is a snippet.** Each one is bounded, and a message longer than
  the bound comes back marked as shortened — the model is told it is holding
  part of something, because a model handed half a message cannot tell.

Relevance here is the same keyword matching the memory segment uses, and says
so for the same reason: §14.3 excludes vector search, and a name that implied
meaning would be a promise this platform does not keep.
"""

from dataclasses import dataclass

#: The most results one search returns. Small on purpose: this is a model
#: deciding what to look at next, and a page it cannot read is a page that only
#: costs context.
MAX_RESULTS = 10
DEFAULT_RESULTS = 5

#: The most one snippet may carry. Long enough to recognise a message by,
#: short enough that ten of them are not a conversation.
MAX_SNIPPET_CHARS = 400

#: The longest query this platform will run. A query longer than this is a
#: paste, and a paste matches nothing useful.
MAX_QUERY_CHARS = 200


class SearchRefused(Exception):
    """A search this platform will not run, and why."""


@dataclass(frozen=True)
class SearchRequest:
    query: str
    limit: int


def request_for(query: str, limit: int | None = None) -> SearchRequest:
    """Validate a search, or refuse it with something a caller can act on.

    The limit is clamped rather than refused — a caller asking for fifty is
    asking for more than this returns, not making a mistake — while an empty
    query is refused, because there is no honest reading of it.
    """
    cleaned = query.strip()
    if not cleaned:
        raise SearchRefused("a search needs something to look for")
    if len(cleaned) > MAX_QUERY_CHARS:
        raise SearchRefused(f"a query is at most {MAX_QUERY_CHARS} characters")
    asked = DEFAULT_RESULTS if limit is None else limit
    if asked < 1:
        raise SearchRefused("a search returns at least one result")
    return SearchRequest(query=cleaned, limit=min(asked, MAX_RESULTS))


@dataclass(frozen=True)
class SearchHit:
    """One past message, as much of it as a result may carry."""

    session_id: str
    run_id: str | None
    sequence: int
    role: str
    snippet: str
    #: True when the message was longer than one snippet. Said rather than
    #: hidden: a model that does not know it is holding part of a message will
    #: answer as though it held all of it.
    shortened: bool


def snippet_of(body: str) -> tuple[str, bool]:
    """A message as a snippet, and whether anything was left out."""
    text = " ".join(body.split())
    if len(text) <= MAX_SNIPPET_CHARS:
        return text, False
    return text[:MAX_SNIPPET_CHARS], True
