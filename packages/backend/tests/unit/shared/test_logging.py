import json

import pytest
import structlog
from tiny_hermes.shared.logging import configure_logging


def test_configure_logging_emits_structured_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()

    structlog.get_logger("test").info("service_started", request_id="req_1")

    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "service_started"
    assert payload["request_id"] == "req_1"
    assert payload["level"] == "info"
