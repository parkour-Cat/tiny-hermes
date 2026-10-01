import json
import logging
import sys
from typing import Any

import pytest
import structlog
from tiny_hermes.shared.logging import FieldsFormatter, configure_logging


def test_configure_logging_emits_structured_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()

    structlog.get_logger("test").info("service_started", request_id="req_1")

    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "service_started"
    assert payload["request_id"] == "req_1"
    assert payload["level"] == "info"


# Every process logs through the standard library, with a format of the
# message alone, so the fields its callers pass as `extra=` were dropped:
# the Worker's run_id and refusal reason, the controller's memory budget. And
# the api never configured logging at all, so its INFO lines were dropped too.


def _record(message: str, **extra: Any) -> logging.LogRecord:
    return logging.getLogger("tiny_hermes.test").makeRecord(
        "tiny_hermes.test", logging.INFO, __file__, 1, message, (), None, extra=extra
    )


def test_the_fields_a_record_carries_are_printed_after_its_message() -> None:
    record = _record("no sandbox yet", run_id="r-1", reason="memory_budget_exhausted")

    assert FieldsFormatter().format(record) == (
        "no sandbox yet run_id=r-1 reason=memory_budget_exhausted"
    )


def test_a_value_that_would_split_the_line_is_quoted() -> None:
    record = _record("commit unknown", error='reset by "peer"', worker_ids=["a", "b"], count=3)

    assert FieldsFormatter().format(record) == (
        'commit unknown error="reset by \\"peer\\"" worker_ids=["a","b"] count=3'
    )


def test_a_record_without_fields_prints_its_message_alone() -> None:
    assert FieldsFormatter().format(_record("sandbox controller stopped")) == (
        "sandbox controller stopped"
    )


def test_a_traceback_follows_the_line_that_carries_the_fields() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.getLogger("t").makeRecord(
            "t",
            logging.ERROR,
            __file__,
            1,
            "slice failed",
            (),
            sys.exc_info(),
            extra={"run_id": "r-1"},
        )

    first, *rest = FieldsFormatter().format(record).splitlines()

    assert first == "slice failed run_id=r-1"
    assert rest[0].startswith("Traceback")


def test_configured_logging_prints_info_and_its_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()

    logging.getLogger("tiny_hermes.runs").info("ending the slice", extra={"run_id": "r-9"})

    assert "ending the slice run_id=r-9" in capsys.readouterr().err


def test_configuring_twice_does_not_print_everything_twice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    configure_logging()

    logging.getLogger("tiny_hermes.runs").info("once")

    assert capsys.readouterr().err.count("once") == 1
