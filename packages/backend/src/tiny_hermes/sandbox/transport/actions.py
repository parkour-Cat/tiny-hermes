"""The Controller's vocabulary, apart from the server so the TLS roles can
name it without importing the socket code."""

#: Actions whose connection switches to Task 5 frames after the control line.
#: `execute_stdin` exists because a `file.write` body must not ride a JSON
#: control line whose whole point is to stay small; `workspace_scan` is here
#: for the mirror-image reason — a full-tree scan of a hundred thousand files
#: is megabytes of metadata, and the drill met a 502-entry scan overflowing
#: the 64 KiB line before any test wrote one that big.
STREAM_ACTIONS = frozenset(
    {
        "workspace_import",
        "workspace_export",
        "workspace_scan",
        "execute_stream",
        "execute_stdin",
    }
)

#: The Controller's whole vocabulary. An action outside it is refused by the
#: server rather than passed on, so a typo cannot become an attribute lookup.
ACTIONS = (
    frozenset(
        {
            "acquire",
            "execute",
            "freeze",
            "thaw",
            "destroy",
            "inspect",
            "keep",
            "cleanup",
            "volume_remove",
        }
    )
    | STREAM_ACTIONS
)
