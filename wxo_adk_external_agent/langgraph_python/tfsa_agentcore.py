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
    {"prompt": "...", "thread_id": "optional", "model": "optional", "stream": false}
"""
import json

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from tfsa_assistant_graph import run_tfsa_assistant_sync, run_tfsa_assistant_stream

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload):
    """AgentCore invocation handler. Returns a string (sync) or an async generator (stream)."""
    prompt = payload.get("prompt", "")
    thread_id = payload.get("thread_id")  # optional, enables multi-turn via the existing cache
    model = payload.get("model")          # optional, used only as response metadata

    # Streaming path: adapt the watsonx-style SSE generator into plain text deltas and
    # let AgentCore handle the text/event-stream framing itself.
    if payload.get("stream"):
        async def gen():
            async for line in run_tfsa_assistant_stream(prompt, thread_id, model):
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
                        yield delta

        return gen()

    # Sync path
    text, _state = run_tfsa_assistant_sync(prompt, thread_id, model)
    return text


if __name__ == "__main__":
    app.run()
