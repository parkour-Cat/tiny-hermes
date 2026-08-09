from uuid import uuid4

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


def request_id(request: Request) -> str:
    value = request.headers.get("X-Request-Id")
    return value if value and len(value) <= 80 else f"req_{uuid4().hex}"


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        current_request_id = request_id(request)
        request.state.request_id = current_request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = current_request_id
        return response
