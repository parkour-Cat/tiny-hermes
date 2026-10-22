"""Tracing is off unless an endpoint is named, and logs carry the trace."""

import logging
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from tiny_hermes.shared.config import Settings
from tiny_hermes.shared.logging import FieldsFormatter
from tiny_hermes.shared.tracing import configure_tracing


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://app:app@db/app",
        "redis_url": "redis://redis:6379/0",
        "s3_endpoint": "http://minio:9000",
        "s3_bucket": "tiny-hermes",
        "s3_access_key": "test-access-key",
        "s3_secret_key": "test-secret-key",
        "session_cookie_secret": "cookie-secret-with-at-least-32-characters",
        "bootstrap_token": "bootstrap-token-with-at-least-32-characters",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_without_an_endpoint_nothing_is_exported() -> None:
    """Design §5.1: off by default, connecting to nothing."""
    assert configure_tracing(_settings(), service="worker") is None


def test_with_an_endpoint_spans_are_sampled_at_the_configured_ratio() -> None:
    provider = configure_tracing(
        _settings(otel_exporter_otlp_endpoint="http://jaeger:4318", otel_traces_sampler_arg=0.1),
        service="worker",
        install=False,
    )
    assert isinstance(provider, TracerProvider)
    assert "0.1" in provider.sampler.get_description()
    assert provider.resource.attributes["service.name"] == "tiny-hermes-worker"
    provider.shutdown()


def test_a_line_logged_inside_a_span_names_its_trace() -> None:
    tracer = TracerProvider().get_tracer("test")
    record = logging.LogRecord("x", logging.INFO, "", 0, "slice started", (), None)
    with trace.use_span(tracer.start_span("run.slice"), end_on_exit=True) as span:
        line = FieldsFormatter().format(record)
        expected = format(span.get_span_context().trace_id, "032x")

    assert f"trace_id={expected}" in line


def test_a_line_logged_outside_any_span_says_nothing_about_traces() -> None:
    record = logging.LogRecord("x", logging.INFO, "", 0, "idle", (), None)
    assert "trace_id" not in FieldsFormatter().format(record)
