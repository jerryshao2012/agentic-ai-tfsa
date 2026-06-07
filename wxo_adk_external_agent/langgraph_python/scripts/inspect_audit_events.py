"""Run one real query through the TFSA agent and print what the audit layer logs.

Shows, for a single turn: the routing decisions, every tool call (name / args / status /
duration), the per-node model reasoning, the LLM calls (prompt identity + tokens + latency),
and the final answer. Useful for eyeballing "what is being logged / which tools fired"
after changes to the reasoning-logging side.

Usage (from wxo_adk_external_agent/langgraph_python, with Bedrock creds active):
    ./.venv/bin/python scripts/inspect_audit_events.py
    ./.venv/bin/python scripts/inspect_audit_events.py "How much room do I have? my user id is user123"
"""
import hashlib
import json
import logging
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_obs

# Capture the agent.audit JSON stream instead of letting it print raw to stdout, so we can
# render a readable summary. Replacing the handler list also keeps the run output clean.
CAPTURED = []


class _Capture(logging.Handler):
    def emit(self, record):
        try:
            CAPTURED.append(json.loads(record.getMessage()))
        except Exception:
            pass


audit_logger = agent_obs.get_audit_logger()  # ensure configured (idempotent)
audit_logger.handlers = [_Capture()]

# Quiet the noisy INFO chatter from the app modules so the summary stands out.
logging.getLogger().setLevel(logging.WARNING)

from tfsa_assistant_graph import (  # noqa: E402
    run_tfsa_assistant_sync, cache, _response_cache_key, extract_user_id,
)


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else (
        "What is the current TFSA contribution limit and what is the penalty "
        "for over-contributing?"
    )

    # Bypass the response cache so the graph actually runs. The cache key is scoped by user_id,
    # so clear both the resolved-user key and the legacy (user-less) key to be safe.
    uid = extract_user_id(query) or "unknown"
    cache.delete(_response_cache_key(query, uid))
    cache.delete(hashlib.sha256(query.encode("UTF-8")).hexdigest())

    print(f"\n=== QUERY ===\n{query}\n")
    response, _state = run_tfsa_assistant_sync(
        query,
        thread_id=f"audit-{uuid.uuid4().hex[:8]}",
        session_id="audit-session",
        message_id=uuid.uuid4().hex[:8],
    )

    by_type = {}
    for ev in CAPTURED:
        by_type.setdefault(ev.get("event_type"), []).append(ev)

    print("=== EVENT COUNTS ===")
    for etype, evs in sorted(by_type.items()):
        print(f"  {etype:20} x{len(evs)}")

    print("\n=== ROUTING DECISIONS ===")
    for ev in by_type.get("routing_decision", []):
        print(f"  {ev.get('node')} -> {ev.get('decision')}   ({ev.get('reason')}; "
              f"data={ev.get('data_selected')})")

    print("\n=== TOOLS CALLED ===")
    tool_events = by_type.get("tool_call", [])
    if not tool_events:
        print("  (none)")
    for ev in tool_events:
        line = f"  {ev.get('tool')}  [{ev.get('status')}]  {ev.get('duration_ms')}ms"
        if ev.get("status") == "error":
            line += f"  error={ev.get('error_type')}: {ev.get('error')}"
        print(line)
    # args come from the matching tool_call_start
    for ev in by_type.get("tool_call_start", []):
        args = json.dumps(ev.get("args"))[:200]
        print(f"    args[{ev.get('tool')}]: {args}")

    print("\n=== DATA SOURCE (profile loads) ===")
    ds_events = by_type.get("data_source", [])
    if not ds_events:
        print("  (no profile loaded this turn)")
    for ev in ds_events:
        print(f"  {ev.get('entity')} for user_id={ev.get('user_id')} -> source={ev.get('source')}")

    print("\n=== MODEL REASONING (agent_reasoning) ===")
    reasoning_events = by_type.get("agent_reasoning", [])
    if not reasoning_events:
        print("  (none logged)")
    for ev in reasoning_events:
        print(f"  [{ev.get('node')}] needs_search={ev.get('needs_search')}")
        print(f"      {ev.get('reasoning') or '(empty)'}")

    print("\n=== ERRORS (node_error / llm_call_error) ===")
    err_events = by_type.get("node_error", []) + by_type.get("llm_call_error", [])
    if not err_events:
        print("  (none)")
    for ev in err_events:
        print(f"  [{ev.get('node', 'llm')}] {ev.get('error_type')}: {ev.get('error')} "
              f"(stage={ev.get('stage')})")

    print("\n=== LLM CALLS ===")
    total_tokens = 0
    starts = {ev.get("run_id"): ev for ev in by_type.get("llm_call_start", [])}
    for ev in by_type.get("llm_call_end", []):
        start = starts.get(ev.get("run_id"), {})
        usage = ev.get("usage") or {}
        total = usage.get("total_tokens") or 0
        total_tokens += total
        ident = start.get("run_name") or start.get("prompt_name") or "advisor/react"
        print(f"  {ident:18} v={start.get('prompt_version')} "
              f"hash={start.get('prompt_hash')} {ev.get('duration_ms')}ms tokens={total}")
        for tc in ev.get("tool_calls") or []:
            print(f"      -> selected tool: {tc.get('name')}({json.dumps(tc.get('args'))})")
    for ev in by_type.get("llm_call_error", []):
        print(f"  LLM ERROR: {ev.get('error_type')}: {ev.get('error')}")
    print(f"  --- total tokens this turn: {total_tokens}")

    print("\n=== FINAL RESPONSE ===")
    print(response)
    print()


if __name__ == "__main__":
    main()
