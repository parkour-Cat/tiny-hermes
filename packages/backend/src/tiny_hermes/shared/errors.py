from typing import Any


class AppError(Exception):
    def __init__(
        self,
        *,
        code: str,
        title: str,
        status: int,
        detail: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.title = title
        self.status = status
        self.detail = detail
        self.context = context or {}
