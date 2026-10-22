"""Request timing by route template (observability design §3.5).

Pure ASGI rather than `BaseHTTPMiddleware`: the route is known only once the
router has matched, and it writes that into the scope this layer passed down.
"""

import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tiny_hermes.shared.metrics import HTTP_REQUEST_SECONDS

#: Anything else is one label, not a series per string a client invents.
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


class RequestMetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        started = time.monotonic()
        status = 500

        async def sending(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, sending)
        finally:
            # An event stream's time is how long somebody watched, not how
            # slow it was; it keeps its own route label, so dashboards can
            # leave it out.
            route: Any = scope.get("route")
            method = str(scope["method"])
            HTTP_REQUEST_SECONDS.labels(
                route=str(getattr(route, "path", "unmatched")),
                method=method if method in _METHODS else "other",
                status_class=f"{status // 100}xx",
            ).observe(time.monotonic() - started)
