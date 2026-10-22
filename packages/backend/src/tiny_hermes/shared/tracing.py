"""OpenTelemetry tracing (observability design §5.1, §5.3).

Off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set: the API's tracer is then the
SDK's no-op, spans cost nothing, and no Run carries a trace context.

Nothing here injects context into an outgoing HTTP request. The platform's
outgoing requests go to model providers and other organisations' APIs, and
an internal trace id sent there is a leak (design §4.3). Context crosses only
two internal boundaries: the Run row, and the controller's control line.
"""

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import extract as extract_trace_context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from tiny_hermes.shared.config import Settings

#: Resolved at each use, so a provider installed after import is the one used.
tracer = trace.get_tracer("tiny_hermes")


def configure_tracing(
    settings: Settings, *, service: str, install: bool = True
) -> TracerProvider | None:
    """Install a provider for this process, or nothing when no endpoint is set.

    Parent-based: a span with a parent follows the parent's decision, so a
    Run sampled at creation is sampled in every slice, and one that was not
    stays out entirely rather than appearing as half a trace.
    """
    endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
    if not endpoint:
        return None
    provider = TracerProvider(
        resource=Resource.create({"service.name": f"tiny-hermes-{service}"}),
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sampler_arg)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    if install:
        trace.set_tracer_provider(provider)
    return provider


def current_traceparent() -> str | None:
    """The current span as a W3C traceparent, or None outside a sampled span."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


def context_from(traceparent: str | None) -> Context | None:
    """A parent context from a stored traceparent; None starts a new trace.

    Extraction validates the format, so a malformed value is a new trace, not
    an error. The function is imported under its own name because the archive
    ban (tests/unit/test_archive_ban.py) forbids the attribute-call spelling
    anywhere in this source, and a ban with one exception grows more.
    """
    if not traceparent:
        return None
    return extract_trace_context({"traceparent": traceparent})
