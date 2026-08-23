"""Every URL the consoles name is a route this platform serves.

The bug that produced this file: the skills page posted to
`/api/v1/skills/{id}/import` for as long as it shipped, and the backend
only ever had `/api/v1/skills/{id}/versions/import`. The button answered
FastAPI's own bare `404` every time somebody pressed it.

Its own unit test could not catch it — the msw stub was written to match
whatever the page called, so the console and its test agreed with each
other and both disagreed with the server. Neither side is wrong on its
own; only the pair is. That is what this file checks, and it is the only
check in the repository that reads both halves.

Deliberately a string sweep rather than a generated client. A generated
client would make the mismatch impossible, which is better — and is a
change to how the whole console talks to the platform, not something to
introduce as a bug fix.
"""

import re
from pathlib import Path

from fastapi.testclient import TestClient

#: Where the two consoles live, relative to the repository root.
CONSOLE_ROOTS = ("apps/web/src", "apps/chat-web/src")

#: URLs a console names that this platform deliberately does not serve.
#: Empty, and meant to stay that way: an entry here is a claim that a page
#: points somewhere else on purpose, which is worth writing down.
ALLOWED_ABSENT: frozenset[str] = frozenset()

#: A template hole — `${workspaceId}`, `${skill.id}`, `${action}`. It may
#: stand for a path parameter *or* for a literal segment the page chooses at
#: runtime (`approve`/`reject`), so it is matched as "one segment, anything".
_HOLE = re.compile(r"\$\{[^}]*\}")
#: URL literals in TypeScript, in quotes or backticks.
_URL = re.compile(r"""["'`](/(?:api/)?v1/[^"'`\s]*)["'`]""")
#: A line that is part of a block comment. Prose names paths too — the
#: chat page's own docstring contrasts `/api/v1/end-user/*` with
#: `/api/v1/{sessions,runs}` — and neither is a call.
_COMMENT = re.compile(r"^\s*(\*|//)")


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _console_urls() -> set[str]:
    found: set[str] = set()
    for root in CONSOLE_ROOTS:
        directory = _repository_root() / root
        for source in directory.rglob("*.ts*"):
            if source.name.endswith((".test.ts", ".test.tsx")):
                # A test's URLs are its own stubs. They are checked by being
                # the thing the page calls, which this file already reads.
                continue
            for line in source.read_text(encoding="utf-8").splitlines():
                if _COMMENT.match(line):
                    continue
                found.update(_URL.findall(line))
    return found


#: One path segment that could be anything: a route's `{param}`, or a
#: console template hole.
ANY = object()


def _served_paths(client: TestClient) -> list[list[object]]:
    """From the app's own OpenAPI document, not from `app.routes`.

    `app.routes` holds objects with no `path` — mounts and websocket routes
    among them — so reading it is how you end up checking the console
    against four paths and concluding everything is broken. The document is
    what this platform publishes as its contract.
    """
    document = client.get("/openapi.json")
    assert document.status_code == 200, document.text
    return [
        [ANY if segment.startswith("{") else segment for segment in path.split("/")]
        for path in document.json()["paths"]
    ]


def _console_segments(url: str) -> list[object]:
    """One console URL as segments, with each hole standing for any one.

    A hole is sometimes a path parameter (`${skillId}`) and sometimes a
    literal the page picks at runtime — `${decision}` is `approve` or
    `reject`, `${action}` is `pause` or `cancel`. Treating it as either
    alone would miss the other, so it matches any single segment. Whether
    those literals are the right ones is a different question from whether
    the shape exists, and this file only asks the second.

    A hole that does not follow a `/` continues the previous segment, and
    in this console that is always a query string
    (`/api/v1/audit-events${suffix}`). Everything from there is dropped —
    which is what a router does with a query anyway.
    """
    without_query = url.split("?")[0]
    trailing = re.search(r"[^/]\$\{", without_query)
    if trailing is not None:
        without_query = without_query[: trailing.start() + 1]
    without_query = without_query.rstrip("/")
    return [
        ANY if _HOLE.search(segment) else segment
        for segment in without_query.split("/")
    ]


def _is_served(url: str, served: list[list[object]]) -> bool:
    asked = _console_segments(url)
    return any(
        len(asked) == len(path)
        and all(
            left is ANY or right is ANY or left == right
            for left, right in zip(asked, path, strict=True)
        )
        for path in served
    )


def test_no_console_url_points_at_a_route_that_does_not_exist(
    client: TestClient,
) -> None:
    served = _served_paths(client)
    assert served, "no routes were read from the application"

    unmatched = [
        url
        for url in sorted(_console_urls())
        if url not in ALLOWED_ABSENT and not _is_served(url, served)
    ]

    assert not unmatched, (
        "These URLs appear in the console and match no route this platform "
        f"serves: {unmatched}. A page pointing at a route that does not exist "
        "answers 404 to whoever presses the button."
    )
