def cursor_is_stale(after: int, earliest: int | None, next_sequence: int) -> bool:
    """Decide whether resuming after ``after`` would silently skip events.

    A cursor of ``N`` means "everything after sequence ``N``", so the first
    sequence the subscriber still needs is ``N + 1``. When nothing is retained,
    the run's next sequence stands in for the earliest one, which keeps a
    caught-up subscriber fresh and a far-behind one stale.
    """
    wanted = after + 1
    return wanted < (next_sequence if earliest is None else earliest)
