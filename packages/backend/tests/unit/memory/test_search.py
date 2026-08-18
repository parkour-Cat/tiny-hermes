"""What a session search may ask for, and what it may hand back.

§14.3 retrieves past sessions **on demand** rather than loading them whole, so
the bounds are the feature rather than defensive trimming: a search that could
return conversations would be the thing the section exists to prevent under a
different name.

The one that matters most is the last group. A snippet that had to be cut says
so, because a model handed part of a message and not told cannot tell — it
answers as though it had the whole thing.
"""

import pytest
from tiny_hermes.memory.domain.search import (
    DEFAULT_RESULTS,
    MAX_QUERY_CHARS,
    MAX_RESULTS,
    MAX_SNIPPET_CHARS,
    SearchRefused,
    request_for,
    snippet_of,
)


def test_a_search_asks_for_something() -> None:
    request = request_for("rollout window")

    assert request.query == "rollout window"
    assert request.limit == DEFAULT_RESULTS


def test_an_empty_query_is_refused_rather_than_read_as_everything() -> None:
    """There is no honest reading of it, and the dishonest one is a dump."""
    with pytest.raises(SearchRefused):
        request_for("   ")


def test_a_query_that_is_a_paste_is_refused() -> None:
    with pytest.raises(SearchRefused):
        request_for("x" * (MAX_QUERY_CHARS + 1))


def test_surrounding_space_is_not_part_of_the_query() -> None:
    assert request_for("  rollout  ").query == "rollout"


def test_asking_for_more_than_a_page_is_clamped_not_refused() -> None:
    """A caller asking for fifty is asking for more than this returns, not
    making a mistake."""
    assert request_for("rollout", MAX_RESULTS + 40).limit == MAX_RESULTS


def test_asking_for_none_is_refused() -> None:
    with pytest.raises(SearchRefused):
        request_for("rollout", 0)


def test_a_short_message_is_its_own_snippet() -> None:
    snippet, shortened = snippet_of("the rollout is on Tuesday")

    assert snippet == "the rollout is on Tuesday"
    assert not shortened


def test_whitespace_is_collapsed_so_a_snippet_reads_as_one_line() -> None:
    snippet, _ = snippet_of("the rollout\n\n  is   on Tuesday")

    assert snippet == "the rollout is on Tuesday"


def test_a_long_message_is_cut_and_says_so() -> None:
    """The whole point of the flag: a model that does not know it is holding
    part of a message will answer as though it held all of it."""
    snippet, shortened = snippet_of("x" * (MAX_SNIPPET_CHARS + 50))

    assert len(snippet) == MAX_SNIPPET_CHARS
    assert shortened


def test_a_message_exactly_at_the_bound_is_not_marked_shortened() -> None:
    snippet, shortened = snippet_of("x" * MAX_SNIPPET_CHARS)

    assert len(snippet) == MAX_SNIPPET_CHARS
    assert not shortened
