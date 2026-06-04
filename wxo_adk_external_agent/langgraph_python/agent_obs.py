# agent_obs.py
"""
Reusable, agent-agnostic observability for LangChain / LangGraph agents.

Emits **one pure-JSON object per log line** to stdout so the host runtime (e.g. AWS Bedrock
AgentCore) ships them to CloudWatch, from where they can be exported to S3 and parsed with a
plain ``json.loads(line)`` — no prefix-stripping needed. A dedicated logger
(``propagate=False``) is used so these lines are NOT wrapped by the application's root-logger
``basicConfig`` format or the OTEL LoggingHandler; instead the OTEL ``trace_id``/``span_id``
are read from the current span and written *into* the JSON.

Captured generically (zero per-agent code):
  * ``invocation_start`` / ``invocation_end``      (via :func:`audited_run`)
  * ``llm_call_start`` (full prompt) / ``llm_call_end`` (raw completion + token usage)
  * ``tool_call``                                   (name, args, result/error, duration)

Integrate from any agent in ~3 lines::

    from agent_obs import AuditCallbackHandler, audited_run

    handler = AuditCallbackHandler(agent="myagent", thread_id=tid, user_id=uid)
    with audited_run(handler, user_input=prompt):
        graph_app.stream(state, config={"callbacks": [handler]})
        handler.set_output(final_text)   # optional: include final output in invocation_end

All callbacks are defensively wrapped: an observability failure must never break the agent.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

try:
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:  # pragma: no cover - langchain always present in our runtimes
    class BaseCallbackHandler:  # minimal shim so import never fails
        pass

try:
    from opentelemetry import trace as _otel_trace
except Exception:  # opentelemetry not importable
    _otel_trace = None

# CloudWatch caps a single log event at 256 KB. Keep individual string fields well under that
# so a giant prompt/completion never drops the whole event. Override via env if needed.
MAX_FIELD_CHARS = int(os.getenv("AGENT_OBS_MAX_FIELD_CHARS", "200000"))

_AUDIT_LOGGER_NAME = "agent.audit"


def get_audit_logger(name: str = _AUDIT_LOGGER_NAME) -> logging.Logger:
    """Return a logger that emits pure-JSON lines to stdout (idempotent)."""
    logger = logging.getLogger(name)
    if not getattr(logger, "_agent_obs_configured", False):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False  # don't double-emit through the root/OTEL handlers
        logger._agent_obs_configured = True  # type: ignore[attr-defined]
    return logger


def _trace_ids() -> tuple[Optional[str], Optional[str]]:
    """(trace_id, span_id) as hex from the current OTEL span, or (None, None)."""
    if _otel_trace is None:
        return None, None
    try:
        ctx = _otel_trace.get_current_span().get_span_context()
        if not ctx or not ctx.trace_id:
            return None, None
        return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
    except Exception:
        return None, None


def _truncate(value: Any) -> Any:
    """Truncate over-long strings so one field can't exceed the CloudWatch event cap."""
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + f"...[truncated {len(value) - MAX_FIELD_CHARS} chars]"
    return value


def _jsonable(obj: Any) -> Any:
    """Best-effort conversion to a JSON-serializable, content-preserving structure."""
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return _truncate(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    # LangChain message / object with a `.content` (prefer the readable text)
    content = getattr(obj, "content", None)
    if content is not None:
        out: dict[str, Any] = {"content": _jsonable(content)}
        mtype = getattr(obj, "type", None) or obj.__class__.__name__
        out["type"] = mtype
        return out
    return _truncate(str(obj))


def log_event(event_type: str, *, agent: Optional[str] = None, thread_id: Optional[str] = None,
              user_id: Optional[str] = None, logger: Optional[logging.Logger] = None,
              **fields: Any) -> None:
    """Emit a single pure-JSON log line. Never raises."""
    try:
        trace_id, span_id = _trace_ids()
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "agent": agent,
            "trace_id": trace_id,
            "span_id": span_id,
            "thread_id": thread_id,
            "user_id": user_id,
        }
        for key, value in fields.items():
            record[key] = _jsonable(value)
        (logger or get_audit_logger()).info(
            json.dumps(record, ensure_ascii=False, default=str)
        )
    except Exception as e:  # observability must never break the caller
        logging.getLogger(__name__).debug("agent_obs log_event failed: %s", e)


def _extract_usage(generation: Any, llm_output: Any) -> Optional[dict]:
    """Pull {input,output,total}_tokens from a chat generation or llm_output."""
    msg = getattr(generation, "message", None)
    usage = getattr(msg, "usage_metadata", None) if msg is not None else None
    if not usage and isinstance(llm_output, dict):
        usage = llm_output.get("usage") or llm_output.get("token_usage")
    if not usage:
        return None
    return {
        "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
        "total_tokens": usage.get("total_tokens", 0),
    }


class AuditCallbackHandler(BaseCallbackHandler):
    """Generic LangChain callback that logs every LLM and tool call as pure-JSON events.

    One instance per invocation: it accumulates per-run token totals and an optional final
    output for the closing ``invocation_end`` event.
    """

    def __init__(self, agent: str, thread_id: Optional[str] = None,
                 user_id: Optional[str] = None):
        self.agent = agent
        self.thread_id = thread_id
        self.user_id = user_id
        self.logger = get_audit_logger()
        self.token_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self._output: Optional[str] = None
        self._llm_starts: dict[str, float] = {}
        self._tool_starts: dict[str, float] = {}

    # -- helpers -------------------------------------------------------------
    def set_user(self, user_id: Optional[str]) -> None:
        if user_id:
            self.user_id = user_id

    def set_output(self, text: Optional[str]) -> None:
        self._output = text

    def _emit(self, event_type: str, **fields: Any) -> None:
        log_event(event_type, agent=self.agent, thread_id=self.thread_id,
                  user_id=self.user_id, logger=self.logger, **fields)

    # -- LLM callbacks -------------------------------------------------------
    def on_chat_model_start(self, serialized, messages, *, run_id=None, parent_run_id=None,
                            **kwargs):
        try:
            self._llm_starts[str(run_id)] = time.time()
            # messages: List[List[BaseMessage]] — flatten the (usually single) batch.
            flat = [m for batch in (messages or []) for m in batch]
            self._emit("llm_call_start", run_id=str(run_id),
                       parent_run_id=str(parent_run_id) if parent_run_id else None,
                       prompt=[_jsonable(m) for m in flat])
        except Exception as e:
            logging.getLogger(__name__).debug("on_chat_model_start failed: %s", e)

    def on_llm_start(self, serialized, prompts, *, run_id=None, parent_run_id=None, **kwargs):
        try:
            self._llm_starts[str(run_id)] = time.time()
            self._emit("llm_call_start", run_id=str(run_id),
                       parent_run_id=str(parent_run_id) if parent_run_id else None,
                       prompt=[_truncate(p) for p in (prompts or [])])
        except Exception as e:
            logging.getLogger(__name__).debug("on_llm_start failed: %s", e)

    def on_llm_end(self, response, *, run_id=None, parent_run_id=None, **kwargs):
        try:
            started = self._llm_starts.pop(str(run_id), None)
            duration_ms = round((time.time() - started) * 1000, 1) if started else None
            llm_output = getattr(response, "llm_output", None)
            completion, usage = [], None
            for batch in getattr(response, "generations", []) or []:
                for gen in batch:
                    completion.append(_truncate(getattr(gen, "text", "") or ""))
                    if usage is None:
                        usage = _extract_usage(gen, llm_output)
            if usage:
                for k in self.token_totals:
                    self.token_totals[k] += usage.get(k, 0) or 0
            self._emit("llm_call_end", run_id=str(run_id), duration_ms=duration_ms,
                       completion=completion, usage=usage)
        except Exception as e:
            logging.getLogger(__name__).debug("on_llm_end failed: %s", e)

    def on_llm_error(self, error, *, run_id=None, parent_run_id=None, **kwargs):
        try:
            self._llm_starts.pop(str(run_id), None)
            self._emit("llm_call_error", run_id=str(run_id),
                       error=str(error), error_type=type(error).__name__)
        except Exception as e:
            logging.getLogger(__name__).debug("on_llm_error failed: %s", e)

    # -- Tool callbacks ------------------------------------------------------
    def on_tool_start(self, serialized, input_str, *, run_id=None, parent_run_id=None,
                      inputs=None, **kwargs):
        try:
            self._tool_starts[str(run_id)] = time.time()
            name = (serialized or {}).get("name") if isinstance(serialized, dict) else None
            self._tool_starts[f"name:{run_id}"] = name  # type: ignore[assignment]
            self._emit("tool_call_start", run_id=str(run_id),
                       parent_run_id=str(parent_run_id) if parent_run_id else None,
                       tool=name, args=_jsonable(inputs if inputs is not None else input_str))
        except Exception as e:
            logging.getLogger(__name__).debug("on_tool_start failed: %s", e)

    def on_tool_end(self, output, *, run_id=None, parent_run_id=None, **kwargs):
        try:
            started = self._tool_starts.pop(str(run_id), None)
            name = self._tool_starts.pop(f"name:{run_id}", None)
            duration_ms = round((time.time() - started) * 1000, 1) if started else None
            self._emit("tool_call", run_id=str(run_id), tool=name, status="success",
                       duration_ms=duration_ms, result=_jsonable(output))
        except Exception as e:
            logging.getLogger(__name__).debug("on_tool_end failed: %s", e)

    def on_tool_error(self, error, *, run_id=None, parent_run_id=None, **kwargs):
        try:
            started = self._tool_starts.pop(str(run_id), None)
            name = self._tool_starts.pop(f"name:{run_id}", None)
            duration_ms = round((time.time() - started) * 1000, 1) if started else None
            self._emit("tool_call", run_id=str(run_id), tool=name, status="error",
                       duration_ms=duration_ms, error=str(error),
                       error_type=type(error).__name__)
        except Exception as e:
            logging.getLogger(__name__).debug("on_tool_error failed: %s", e)


@contextmanager
def audited_run(handler: AuditCallbackHandler, user_input: str):
    """Bracket one invocation with ``invocation_start`` / ``invocation_end`` events.

    Emits the raw input on entry and, on exit, the accumulated token totals, wall-clock
    duration, status, and (if set via :meth:`AuditCallbackHandler.set_output`) the final output.
    """
    invocation_id = str(uuid.uuid4())
    start = time.time()
    handler._emit("invocation_start", invocation_id=invocation_id, input=user_input)
    status = "success"
    try:
        yield handler
    except Exception as e:
        status = "error"
        handler._emit("invocation_error", invocation_id=invocation_id,
                      error=str(e), error_type=type(e).__name__)
        raise
    finally:
        handler._emit("invocation_end", invocation_id=invocation_id, status=status,
                      duration_ms=round((time.time() - start) * 1000, 1),
                      token_usage=handler.token_totals, output=handler._output)
