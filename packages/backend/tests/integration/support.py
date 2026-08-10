"""Types shared by the integration tests.

A fixture that returns a callable needs a name its users can annotate, and a
``Callable[..., ...]`` alias is not that name: it erases the argument names and
it erases the coroutine type ``asyncio.create_task`` demands, so a test that
subscribes in the background loses every guarantee about what it read.
"""

from collections.abc import Coroutine
from typing import Any, Protocol

import httpx2 as httpx


class ReadStream(Protocol):
    """Read one Server-Sent Events stream to its end."""

    def __call__(
        self, url: str, headers: dict[str, str] | None = None
    ) -> Coroutine[Any, Any, list[httpx.ServerSentEvent]]: ...


class EventsUrl(Protocol):
    """Address a Run's event stream, defaulting to the selected workspace."""

    def __call__(
        self, run_id: Any, cursor: int | None = None, workspace_id: str = ""
    ) -> str: ...
