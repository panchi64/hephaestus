"""Process-wide tracing of every pydantic-ai agent, kept in-process.

Spans go to a private `TracerProvider` with one processor, :class:`SpanCollector`; nothing
is exported and the global OpenTelemetry provider is left alone. Content capture is off, so
prompts, tool arguments and results never enter a span. Turning instrumentation on is also
what stops pydantic-ai's "observability: off" startup banner.
"""

from __future__ import annotations

from collections.abc import Callable

from opentelemetry.sdk.trace import TracerProvider
from pydantic_ai import Agent, InstrumentationSettings

from .collector import SpanCollector
from .sink import SpanSink

collector = SpanCollector()
_provider: TracerProvider | None = None


def install_tracing(sink: SpanSink) -> Callable[[], None]:
    """Route every agent's spans to ``sink``; returns the undo.

    Idempotent across apps in one process: the provider and the instrumentation default
    are set once, and only the delivery target follows the latest app."""
    global _provider
    if _provider is None:
        provider = TracerProvider()
        provider.add_span_processor(collector)
        _provider = provider
        Agent.instrument_all(
            InstrumentationSettings(
                tracer_provider=provider,
                include_content=False,
                include_binary_content=False,
                include_model_request_parameters=False,
            )
        )
    collector.retarget(sink.deliver)

    def uninstall() -> None:
        if collector.target_is(sink.deliver):
            collector.retarget(None)

    return uninstall


def tracing_active() -> bool:
    return _provider is not None
