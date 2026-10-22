"""One in-memory span exporter for the whole test process.

A process sets its tracer provider once; the first test to ask installs this
one, always sampling, and every later test reads and clears the same exporter.
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_EXPORTER = InMemorySpanExporter()
_installed = False


def recorded() -> InMemorySpanExporter:
    global _installed
    if not _installed:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        trace.set_tracer_provider(provider)
        _installed = True
    _EXPORTER.clear()
    return _EXPORTER


def named(exporter: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [span for span in exporter.get_finished_spans() if span.name == name]


def trace_id_of(traceparent: str) -> int:
    """W3C `version-traceid-spanid-flags`."""
    return int(traceparent.split("-")[1], 16)
