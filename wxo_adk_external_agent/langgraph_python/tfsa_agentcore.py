# tfsa_agentcore.py
"""
Amazon Bedrock AgentCore Runtime entrypoint for the TFSA LangGraph assistant.

This is a thin wrapper around the existing multi-agent workflow defined in
`tfsa_assistant_graph.py`. It reuses `run_tfsa_assistant_sync` (non-streaming) and
`run_tfsa_assistant_stream` (streaming) so the same agent that backs the IBM watsonx
Orchestrate FastAPI app can be hosted on AWS Bedrock AgentCore Runtime.

The LLM provider is selected via `config.AI_SERVICES_PROVIDER` (default `bedrock`); see
`config.py` / `initialize_llm()` for the Bedrock branch.

Run locally:
    python tfsa_agentcore.py          # serves /invocations + /ping on :8080

Payload shape:
    {"prompt": "...", "thread_id": "optional", "model": "optional", "stream": false,
     "session_id": "optional", "message_id": "optional"}

session_id groups many messages in one conversation; message_id identifies a single
turn. Both are accepted from the payload (client-controlled) and fall back to the
AgentCore runtime session id, then a generated UUID, so logs always carry both.
"""
import asyncio
import json
import os
import uuid
from bedrock_agentcore.runtime import BedrockAgentCoreApp

# Prevent graph image generation when this module imports the workflow graph.
os.environ.setdefault("TFSA_SKIP_GRAPH_IMAGE", "1")

from tfsa_assistant_graph import run_tfsa_assistant_sync, run_tfsa_assistant_stream

app = BedrockAgentCoreApp()

_THROTTLE_RETRIES = int(os.getenv("AGENTCORE_THROTTLE_RETRIES", "1"))
_THROTTLE_BACKOFF_SECONDS = float(os.getenv("AGENTCORE_THROTTLE_BACKOFF_SECONDS", "0.8"))


def _looks_like_throttled_response(text: str) -> bool:
    value = (text or "").lower()
    return (
            "temporarily busy" in value
            or "rate limit" in value
            or "throttlingexception" in value
    )


def _resolve_ids(payload, context):
    """Return (session_id, message_id): payload first, then AgentCore context, then UUID."""
    ctx_session = getattr(context, "session_id", None) if context is not None else None
    session_id = payload.get("session_id") or ctx_session or str(uuid.uuid4())
    message_id = payload.get("message_id") or str(uuid.uuid4())
    return session_id, message_id


@app.entrypoint
async def invoke(payload, context=None):
    """AgentCore invocation handler. Returns a string (sync) or an async generator (stream)."""
    prompt = payload.get("prompt", "")
    thread_id = payload.get("thread_id")  # optional, enables multi-turn via the existing cache
    model = payload.get("model")  # optional, used only as response metadata
    session_id, message_id = _resolve_ids(payload, context)

    # Streaming path: adapt the watsonx-style SSE generator into plain text deltas and
    # let AgentCore handle the text/event-stream framing itself.
    if payload.get("stream"):
        async def gen():
            emitted = False
            async for line in run_tfsa_assistant_stream(prompt, thread_id, model,
                                                        session_id=session_id,
                                                        message_id=message_id):
                if not line.startswith("data: "):  # skip ":heartbeat" keep-alive lines
                    continue
                body = line[len("data: "):].strip()
                if body == "[DONE]":
                    break
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    continue
                for choice in data.get("choices", []):
                    delta = choice.get("delta", {}).get("content")
                    if delta:
                        emitted = True
                        yield delta

            # Prevent empty streamed responses reaching the caller.
            if not emitted:
                yield "No response was generated for this request. Please retry."

        return gen()

    # Sync path: retry a throttled response once (or per env setting) with short backoff.
    text = ""
    state: dict = {}
    for attempt in range(_THROTTLE_RETRIES + 1):
        text, state = await asyncio.to_thread(
            run_tfsa_assistant_sync, prompt, thread_id, model,
            session_id=session_id, message_id=message_id
        )
        text = text if isinstance(text, str) else str(text or "")
        if not _looks_like_throttled_response(text):
            break
        if attempt < _THROTTLE_RETRIES:
            await asyncio.sleep(_THROTTLE_BACKOFF_SECONDS * (2 ** attempt))

    if not text.strip():
        text = "No response was generated for this request. Please retry."

    # Return a JSON object carrying the structured trace by default; opt out with
    # {"include_trace": false} to get the legacy plain-string response.
    if payload.get("include_trace", True):
        return {"response": text, "trace": (state or {}).get("trace", {})}
    return text


if __name__ == "__main__":
    app.run()
