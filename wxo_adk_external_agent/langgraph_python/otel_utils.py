# otel_utils.py
"""Lightweight OpenTelemetry helpers for per-agent tracing of the TFSA workflow.

Spans become real telemetry only when an OTEL tracer provider/exporter is configured
(as it is in the AgentCore runtime when AGENT_OBSERVABILITY_ENABLED=true with the
aws-opentelemetry-distro package). Everywhere else these are cheap no-ops, so the
module is safe to import and call locally with no telemetry backend.

ADOT auto-instruments the Bedrock/boto3 calls (token usage + latency per LLM call),
so this module focuses on the agent-level structure: one span per graph node, the
supervisor's routing decision, and recorded errors.
"""
import functools
import logging

try:
    from opentelemetry import trace
    _tracer = trace.get_tracer("tfsa.agents")
except Exception as e:  # opentelemetry not importable at all
    logging.warning(f"OpenTelemetry unavailable, tracing disabled: {e}")
    trace = None
    _tracer = None


def set_attr(key, value):
    """Set an attribute on the current span (no-op if tracing is off)."""
    if trace is None or value is None:
        return
    try:
        trace.get_current_span().set_attribute(key, value)
    except Exception:
        pass


def record_error(exc):
    """Record an exception on the current span without raising."""
    if trace is None:
        return
    try:
        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_attribute("error", True)
    except Exception:
        pass


def traced(name):
    """Decorator that wraps a graph node in a span named ``agent.<name>``.

    Surfaces the routing ``intent`` as a span attribute when the node returns one,
    and records any exception the node raises.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(state, *args, **kwargs):
            if _tracer is None:
                return fn(state, *args, **kwargs)
            with _tracer.start_as_current_span(f"agent.{name}") as span:
                try:
                    result = fn(state, *args, **kwargs)
                    if isinstance(result, dict) and result.get("intent"):
                        span.set_attribute("tfsa.intent", result["intent"])
                    return result
                except Exception as e:
                    span.record_exception(e)
                    span.set_attribute("error", True)
                    raise
        return wrapper
    return deco
