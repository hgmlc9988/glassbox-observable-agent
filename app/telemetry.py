"""
Glassbox — OpenTelemetry setup.

Spans go to Langfuse over OTLP/HTTP. Console export is available behind an
env var for debugging.

Nothing in the agent or tools imports anything Langfuse-specific. The one place
vendor knowledge lives is set_input / set_output below, which write both the
vendor-neutral attribute and the Langfuse one. Swapping to Cloud Trace,
Honeycomb or a local collector is a change to this file only.

Langfuse notes:
  - HTTP with protobuf only. gRPC is not supported.
  - The traces path is /api/public/otel/v1/traces.
  - Spans carrying gen_ai.* attributes render as generations with model,
    token and cost data.
  - Input and output are read from the observation, in priority order:
    langfuse.observation.input, gen_ai.prompt, input.value. Trace-level
    input/output is deprecated, so these go on the root span.
"""

import atexit
import os

from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

load_dotenv()

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "glassbox-research")
WORKFLOW_NAME = "glassbox-research"

LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "https://us.cloud.langfuse.com")
LANGFUSE_AUTH = os.environ.get("LANGFUSE_AUTH", "")
CONSOLE_EXPORT = os.environ.get("OTEL_CONSOLE_EXPORT", "false").lower() == "true"

# Attribute values are capped so a 12,000 character page cannot bloat a span.
MAX_ATTRIBUTE_CHARS = 4000

# Gemini 2.5 Flash pricing, US dollars per million tokens.
# Check current pricing before trusting these numbers in a demo.
COST_PER_M_INPUT = 0.30
COST_PER_M_OUTPUT = 2.50

_provider: TracerProvider | None = None


def init_tracing() -> None:
    """Set up the tracer provider. Safe to call more than once."""
    global _provider
    if _provider is not None:
        return

    resource = Resource.create({"service.name": SERVICE_NAME})
    provider = TracerProvider(resource=resource)

    if LANGFUSE_AUTH:
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=f"{LANGFUSE_HOST}/api/public/otel/v1/traces",
                    headers={
                        "Authorization": f"Basic {LANGFUSE_AUTH}",
                        "x-langfuse-ingestion-version": "4",
                    },
                )
            )
        )
    else:
        print("WARNING: LANGFUSE_AUTH not set, traces are not being exported")

    if CONSOLE_EXPORT:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _provider = provider

    # Short scripts exit before the batch processor flushes on its own timer.
    # Without this, the last spans of every run are silently lost.
    atexit.register(shutdown_tracing)


def shutdown_tracing() -> None:
    """Flush pending spans and shut the provider down."""
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None


def tracer() -> trace.Tracer:
    init_tracing()
    return trace.get_tracer("glassbox")


def _truncate(text: str) -> str:
    if len(text) <= MAX_ATTRIBUTE_CHARS:
        return text
    return text[:MAX_ATTRIBUTE_CHARS] + f"... [truncated, {len(text)} chars total]"


def set_input(span: trace.Span, value: str) -> None:
    """Record what went into this span.

    Written twice on purpose: input.value is the vendor-neutral convention,
    langfuse.observation.input is what Langfuse reads first. Any backend that
    understands either one will render this span correctly.
    """
    clean = _truncate(value)
    span.set_attribute("input.value", clean)
    span.set_attribute("langfuse.observation.input", clean)


def set_output(span: trace.Span, value: str) -> None:
    """Record what came out of this span."""
    clean = _truncate(value)
    span.set_attribute("output.value", clean)
    span.set_attribute("langfuse.observation.output", clean)


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Dollar cost of one model call."""
    return (
        input_tokens * COST_PER_M_INPUT / 1_000_000
        + output_tokens * COST_PER_M_OUTPUT / 1_000_000
    )


def record_usage(span: trace.Span, response) -> None:
    """Copy token counts off a LangChain response onto a chat span."""
    usage = getattr(response, "usage_metadata", None) or {}
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
    span.set_attribute("glassbox.cost_usd", estimate_cost(input_tokens, output_tokens))