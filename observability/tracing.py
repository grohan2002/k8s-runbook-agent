"""OpenTelemetry tracing integration.

Provides distributed traces for the diagnosis pipeline:
  Alert → Runbook Search → Claude Loop (per-round spans) → Tool Calls → Fix Execution

When OTEL_EXPORTER_OTLP_ENDPOINT is set, traces are exported via OTLP.
Otherwise, tracing is a no-op (zero overhead in production without a collector).

Usage:
    from ..observability.tracing import tracer, optional_span

    with tracer.start_as_current_span("investigate") as span:
        span.set_attribute("session.id", session.id)
        ...

Or as a no-op-safe wrapper:
    async with optional_span("tool_call", {"tool": name}):
        result = await tool(args)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager, contextmanager
from typing import Any

logger = logging.getLogger(__name__)

# Try to import OpenTelemetry — gracefully degrade if not installed
_otel_available = False

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource

    _otel_available = True
except ImportError:
    pass


def init_tracing(service_name: str = "k8s-runbook-agent") -> None:
    """Initialize OpenTelemetry tracing if the SDK is installed and configured.

    Call this once at startup (in server.py lifespan).
    """
    if not _otel_available:
        logger.info("OpenTelemetry SDK not installed — tracing disabled")
        return

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set — tracing disabled")
        return

    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        logger.info("OpenTelemetry tracing initialized (endpoint=%s)", endpoint)
    except Exception:
        logger.exception("Failed to initialize OpenTelemetry tracing")


def get_tracer(name: str = "k8s-runbook-agent") -> Any:
    """Get an OpenTelemetry tracer, or a no-op tracer if OTel isn't available."""
    if _otel_available:
        return trace.get_tracer(name)
    return _NoOpTracer()


@contextmanager
def optional_span(name: str, attributes: dict[str, Any] | None = None):
    """Context manager that creates a span if OTel is available, else no-op."""
    if _otel_available:
        with tracer.start_as_current_span(name) as span:
            if attributes:
                for k, v in attributes.items():
                    span.set_attribute(k, str(v))
            yield span
    else:
        yield _NoOpSpan()


@asynccontextmanager
async def async_optional_span(name: str, attributes: dict[str, Any] | None = None):
    """Async context manager that creates a span if OTel is available, else no-op."""
    if _otel_available:
        with tracer.start_as_current_span(name) as span:
            if attributes:
                for k, v in attributes.items():
                    span.set_attribute(k, str(v))
            yield span
    else:
        yield _NoOpSpan()


# ---------------------------------------------------------------------------
# No-op fallbacks (zero overhead when OTel is not installed)
# ---------------------------------------------------------------------------
class _NoOpSpan:
    """Dummy span that accepts all method calls and does nothing."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        pass

    def set_status(self, *args, **kwargs) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _NoOpTracer:
    """Dummy tracer that returns no-op spans."""

    def start_as_current_span(self, name: str, **kwargs):
        return _NoOpSpan()

    def start_span(self, name: str, **kwargs):
        return _NoOpSpan()


# Module-level tracer instance — must be after _NoOpTracer definition
tracer = get_tracer()
