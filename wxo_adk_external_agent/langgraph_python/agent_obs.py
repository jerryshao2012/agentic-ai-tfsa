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

import contextvars
import hashlib
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

# Tool names whose results are surfaced as `retrieved_items` in the built trace.
_RETRIEVAL_TOOLS = {"retrieve_user_profile", "search_cra_tfsa_policy_duck_duck_go"}
# Event fields too heavy/sensitive to echo back in the response trace (kept in CloudWatch).
_RAW_TRACE_DROP_FIELDS = ("prompt", "completion")


class TraceCollector:
    """Accumulates every emitted log_event for one invocation and builds a structured trace.

    Activated for the duration of :func:`audited_run` via the ``_active_trace`` contextvar, so
    *all* events that funnel through :func:`log_event` (handler callbacks, ``routing_decision``,
    in-graph ``node_error``, …) are captured with no per-call-site changes. :meth:`build` derives
    the typed views (agents_called / handoffs / tool_calls / …) from the raw event stream.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def add(self, record: dict) -> None:
        self.events.append(record)

    def build(self) -> dict:
        agents_called: list[str] = []
        handoffs: list[dict] = []
        tool_calls: list[dict] = []
        retrieved_items: list[dict] = []
        errors: list[dict] = []
        latency_ms: Optional[float] = None
        # tool args live on tool_call_start; join them onto the tool_call (end) event by run_id.
        tool_args: dict[str, Any] = {}

        for ev in self.events:
            et = ev.get("event_type")
            if et == "agent_node_output":
                node = ev.get("node")
                if node and node not in agents_called:
                    agents_called.append(node)
            elif et == "routing_decision":
                handoffs.append({k: ev.get(k) for k in
                                 ("node", "decision", "reason", "data_selected")})
            elif et == "tool_call_start":
                if ev.get("run_id") is not None:
                    tool_args[ev["run_id"]] = ev.get("args")
            elif et == "tool_call":
                tool = ev.get("tool")
                entry = {"tool": tool, "status": ev.get("status"),
                         "duration_ms": ev.get("duration_ms"),
                         "args": tool_args.get(ev.get("run_id"))}
                if ev.get("status") == "error":
                    entry["error"] = ev.get("error")
                    errors.append({"scope": "tool", "tool": tool,
                                   "error": ev.get("error"),
                                   "error_type": ev.get("error_type")})
                else:
                    entry["result"] = ev.get("result")
                tool_calls.append(entry)
                if tool in _RETRIEVAL_TOOLS and ev.get("status") != "error":
                    retrieved_items.append({"source": tool, "result": ev.get("result")})
            elif et in ("llm_call_error", "node_error", "invocation_error"):
                errors.append({"scope": et, "node": ev.get("node"),
                               "error": ev.get("error"),
                               "error_type": ev.get("error_type")})
            elif et == "invocation_end":
                latency_ms = ev.get("duration_ms")

        raw_trace = [{k: v for k, v in ev.items() if k not in _RAW_TRACE_DROP_FIELDS}
                     for ev in self.events]

        return {
            "agents_called": agents_called,
            "handoffs": handoffs,
            "tool_calls": tool_calls,
            "retrieved_items": retrieved_items,
            "memory_reads": [],   # no memory store yet; key kept for schema stability
            "memory_writes": [],  # no memory store yet; key kept for schema stability
            "errors": errors,
            "latency_ms": latency_ms,
            "raw_trace": raw_trace,
        }


# Active trace collector for the current invocation (None outside an audited_run).
_active_trace: contextvars.ContextVar[Optional[TraceCollector]] = contextvars.ContextVar(
    "agent_obs_trace", default=None)


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


def _prompt_text(prompt: Any) -> str:
    """Flatten a logged prompt (list of message dicts or strings) to a single string."""
    parts: list[str] = []
    for item in prompt or []:
        if isinstance(item, dict):
            parts.append(str(item.get("content", "")))
        else:
            parts.append(str(item))
    return "\n".join(parts)


def _prompt_identity(kwargs: dict, prompt: Any) -> dict:
    """Extract prompt name/version/role + a content hash for an llm_call_start event.

    Agents tag their LLM calls via LangChain config metadata, e.g.
    ``llm.invoke(prompt, config={"run_name": "document_agent",
        "metadata": {"prompt_name": "document_policy_expert",
                     "prompt_version": "v1", "prompt_role": "system"}})``.
    The metadata/tags/run name arrive here in callback kwargs. prompt_hash lets you
    detect prompt drift / injection without diffing full text.
    """
    out: dict[str, Any] = {}
    try:
        meta = kwargs.get("metadata") or {}
        if isinstance(meta, dict):
            for key in ("prompt_name", "prompt_version", "prompt_role"):
                if meta.get(key) is not None:
                    out[key] = meta[key]
        run_name = kwargs.get("name") or kwargs.get("run_name")
        if run_name:
            out["run_name"] = run_name
        out.setdefault("prompt_role", "system")
        out["prompt_hash"] = hashlib.sha256(
            _prompt_text(prompt).encode("utf-8", "ignore")).hexdigest()[:12]
    except Exception as e:  # never break logging over metadata extraction
        logging.getLogger(__name__).debug("_prompt_identity failed: %s", e)
    return out


def log_event(event_type: str, *, agent: Optional[str] = None, thread_id: Optional[str] = None,
              user_id: Optional[str] = None, session_id: Optional[str] = None,
              message_id: Optional[str] = None, logger: Optional[logging.Logger] = None,
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
            "session_id": session_id,
            "message_id": message_id,
            "thread_id": thread_id,
            "user_id": user_id,
        }
        for key, value in fields.items():
            record[key] = _jsonable(value)
        # Tee into the active trace collector (if any) so the response can carry a structured
        # trace. log_event is the single chokepoint for all events, so this captures everything.
        collector = _active_trace.get()
        if collector is not None:
            collector.add(record)
        (logger or get_audit_logger()).info(
            json.dumps(record, ensure_ascii=False, default=str)
        )
    except Exception as e:  # observability must never break the caller
        logging.getLogger(__name__).debug("agent_obs log_event failed: %s", e)


def _extract_thinking(generation: Any) -> Optional[str]:
    """Pull native extended-thinking / reasoning text from a chat generation, if present.

    ChatBedrockConverse surfaces Claude reasoning as content blocks of type
    ``reasoning_content`` (``{"type": "reasoning_content", "reasoning_content": {"text": ...}}``)
    on the AIMessage, and some providers stash it in ``additional_kwargs["reasoning_content"]``.
    Returns the concatenated reasoning text, or None when the call had no thinking enabled.
    """
    msg = getattr(generation, "message", None)
    if msg is None:
        return None
    parts: list[str] = []
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("reasoning_content", "thinking"):
                rc = block.get("reasoning_content") or block.get("thinking") or {}
                text = rc.get("text") if isinstance(rc, dict) else rc
                if text:
                    parts.append(str(text))
    extra = getattr(msg, "additional_kwargs", None)
    if isinstance(extra, dict):
        rc = extra.get("reasoning_content")
        if isinstance(rc, dict) and rc.get("text"):
            parts.append(str(rc["text"]))
        elif isinstance(rc, str) and rc:
            parts.append(rc)
    return "\n".join(parts) if parts else None


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
                 user_id: Optional[str] = None, session_id: Optional[str] = None,
                 message_id: Optional[str] = None):
        self.agent = agent
        self.thread_id = thread_id
        self.user_id = user_id
        # session_id groups many messages in one conversation; message_id identifies a
        # single request/turn. Both are stamped on EVERY event via _emit().
        self.session_id = session_id
        self.message_id = message_id
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

    def get_trace(self) -> dict:
        """Build the structured trace for this invocation (empty dict if no run was bracketed)."""
        collector = getattr(self, "_trace_collector", None)
        return collector.build() if collector is not None else {}

    def _emit(self, event_type: str, **fields: Any) -> None:
        log_event(event_type, agent=self.agent, thread_id=self.thread_id,
                  user_id=self.user_id, session_id=self.session_id,
                  message_id=self.message_id, logger=self.logger, **fields)

    # -- LLM callbacks -------------------------------------------------------
    def on_chat_model_start(self, serialized, messages, *, run_id=None, parent_run_id=None,
                            **kwargs):
        try:
            self._llm_starts[str(run_id)] = time.time()
            # messages: List[List[BaseMessage]] — flatten the (usually single) batch.
            flat = [m for batch in (messages or []) for m in batch]
            prompt = [_jsonable(m) for m in flat]
            self._emit("llm_call_start", run_id=str(run_id),
                       parent_run_id=str(parent_run_id) if parent_run_id else None,
                       prompt=prompt, **_prompt_identity(kwargs, prompt))
        except Exception as e:
            logging.getLogger(__name__).debug("on_chat_model_start failed: %s", e)

    def on_llm_start(self, serialized, prompts, *, run_id=None, parent_run_id=None, **kwargs):
        try:
            self._llm_starts[str(run_id)] = time.time()
            prompt = [_truncate(p) for p in (prompts or [])]
            self._emit("llm_call_start", run_id=str(run_id),
                       parent_run_id=str(parent_run_id) if parent_run_id else None,
                       prompt=prompt, **_prompt_identity(kwargs, prompt))
        except Exception as e:
            logging.getLogger(__name__).debug("on_llm_start failed: %s", e)

    def on_llm_end(self, response, *, run_id=None, parent_run_id=None, **kwargs):
        try:
            started = self._llm_starts.pop(str(run_id), None)
            duration_ms = round((time.time() - started) * 1000, 1) if started else None
            llm_output = getattr(response, "llm_output", None)
            completion, usage, thinking, tool_calls = [], None, None, []
            for batch in getattr(response, "generations", []) or []:
                for gen in batch:
                    completion.append(_truncate(getattr(gen, "text", "") or ""))
                    if usage is None:
                        usage = _extract_usage(gen, llm_output)
                    if thinking is None:
                        thinking = _extract_thinking(gen)
                    # The model's tool-selection lives on the message, not in `text` — capture it
                    # so "the LLM chose tool X(args)" is explicit on this event.
                    msg = getattr(gen, "message", None)
                    for tc in (getattr(msg, "tool_calls", None) or []):
                        if isinstance(tc, dict):
                            tool_calls.append({"name": tc.get("name"), "args": _jsonable(tc.get("args"))})
                        else:
                            tool_calls.append({"name": getattr(tc, "name", None),
                                               "args": _jsonable(getattr(tc, "args", None))})
            if usage:
                for k in self.token_totals:
                    self.token_totals[k] += usage.get(k, 0) or 0
            extra = {"thinking": _truncate(thinking)} if thinking else {}
            # `thinking` is included only when native extended thinking was enabled for the call.
            if tool_calls:
                extra["tool_calls"] = tool_calls
            self._emit("llm_call_end", run_id=str(run_id), duration_ms=duration_ms,
                       completion=completion, usage=usage, **extra)
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
def audited_run(handler: AuditCallbackHandler, user_input: str,
                message_id: Optional[str] = None):
    """Bracket one invocation with ``invocation_start`` / ``invocation_end`` events.

    Emits the raw input on entry and, on exit, the accumulated token totals, wall-clock
    duration, status, and (if set via :meth:`AuditCallbackHandler.set_output`) the final output.

    ``invocation_id`` is set to the caller's ``message_id`` (falling back to the handler's
    message_id, else a fresh UUID) so a turn's start/end records share one id for joining.
    """
    invocation_id = message_id or handler.message_id or str(uuid.uuid4())
    start = time.time()
    # Activate a trace collector for this invocation so every log_event is teed into it; the
    # caller can read the structured trace afterwards via handler.get_trace().
    collector = TraceCollector()
    handler._trace_collector = collector  # type: ignore[attr-defined]
    token = _active_trace.set(collector)
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
        _active_trace.reset(token)
