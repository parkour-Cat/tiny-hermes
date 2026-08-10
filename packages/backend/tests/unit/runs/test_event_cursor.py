from tiny_hermes.runs.domain.event_cursor import cursor_is_stale


def test_a_fresh_subscriber_is_never_stale() -> None:
    assert cursor_is_stale(0, earliest=1, next_sequence=4) is False


def test_a_cursor_that_meets_the_window_exactly_is_fresh() -> None:
    assert cursor_is_stale(2, earliest=3, next_sequence=6) is False


def test_a_cursor_below_the_window_is_stale() -> None:
    assert cursor_is_stale(0, earliest=3, next_sequence=6) is True


def test_a_cursor_ahead_of_the_window_is_fresh() -> None:
    """A caught-up subscriber has nothing to resynchronize."""
    assert cursor_is_stale(5, earliest=3, next_sequence=6) is False


def test_an_empty_history_measures_against_the_next_sequence() -> None:
    assert cursor_is_stale(0, earliest=None, next_sequence=4) is True
    assert cursor_is_stale(3, earliest=None, next_sequence=4) is False


def test_an_untouched_run_with_no_events_is_fresh() -> None:
    assert cursor_is_stale(0, earliest=None, next_sequence=1) is False
