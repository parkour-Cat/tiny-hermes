import json
import logging
from typing import Any

import structlog

#: What every LogRecord has; anything else on a record came from `extra=`.
_STANDARD = frozenset(vars(logging.LogRecord("", logging.INFO, "", 0, "", (), None))) | {
    "message",
    "asctime",
}


class FieldsFormatter(logging.Formatter):
    """The message, then each `extra=` field as `key=value`, on one line.

    The format used to be the message alone, so every field a caller passed —
    a run_id, a refusal reason, the controller's memory budget — was dropped.
    One line per record, greppable, readable in `docker compose logs`; a
    traceback still follows on the lines below.
    """

    def __init__(self) -> None:
        super().__init__("%(message)s")

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802 - stdlib's name
        fields = [
            f"{key}={_value(value)}" for key, value in vars(record).items() if key not in _STANDARD
        ]
        return " ".join([super().formatMessage(record), *fields])


def _value(value: Any) -> str:
    if isinstance(value, str):
        return value if value and not any(c in value for c in ' "=\n\t') else json.dumps(value)
    if value is None or isinstance(value, bool | int | float):
        return str(value)
    return json.dumps(value, separators=(",", ":"), default=str)


def configure_logging() -> None:
    root = logging.getLogger()
    # Once per process, however many times it is called: a second handler
    # would print every line twice.
    if not any(isinstance(handler.formatter, FieldsFormatter) for handler in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(FieldsFormatter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ]
    )
