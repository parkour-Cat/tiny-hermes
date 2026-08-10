from typing import Any


class AuditedDenial(Exception):
    """An expected refusal whose audit record must outlive the refusal.

    Request dependencies commit the surrounding transaction for this class only,
    then re-raise it; every other exception still rolls the transaction back.
    """


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
