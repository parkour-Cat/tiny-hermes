"""Request timing and the request's span (observability design §3.5, §5.3).

Pure ASGI rather than `BaseHTTPMiddleware`: the route is known only once the
router has matched, and it writes that into the scope this layer passed down.

Every request starts a new trace. A `traceparent` the client sent is not
read: it would let any caller choose trace ids and whether they are sampled.
"""

import time
from typing import Any

from opentelemetry.context import Context
from opentelemetry.trace import SpanKind
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tiny_hermes.shared.metrics import HTTP_REQUEST_SECONDS
from tiny_hermes.shared.tracing import tracer

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

        raw_method = str(scope["method"])
        method = raw_method if raw_method in _METHODS else "other"
        with tracer.start_as_current_span(
            method, kind=SpanKind.SERVER, context=Context()
        ) as span:
            try:
                await self._app(scope, receive, sending)
            finally:
                route: Any = scope.get("route")
                template = str(getattr(route, "path", "unmatched"))
                span.update_name(f"{method} {template}")
                span.set_attribute("http.route", template)
                span.set_attribute("http.response.status_code", status)
                # An event stream's time is how long somebody watched, not
                # how slow it was; it keeps its own route label, so dashboards
                # can leave it out.
                HTTP_REQUEST_SECONDS.labels(
                    route=template, method=method, status_class=f"{status // 100}xx"
                ).observe(time.monotonic() - started)
