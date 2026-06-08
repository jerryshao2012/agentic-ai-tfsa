#!/usr/bin/env python3
"""Local smoke test for the observability events (no deploy needed).

Drives representative queries through ``run_tfsa_assistant_sync`` and verifies the events added
for plan / data-selection / reasoning / errors actually fire:
  * routing_decision  — which branch the graph took and why (the agent plan)
  * agent_reasoning   — each LLM node's rationale (audit-only)
  * node_error        — structured node-level failures
  * llm_call_end      — now carries `thinking` when ENABLE_THINKING + a Claude model are used

It also asserts the reasoning text never leaks into the user-facing reply.

Requires the runtime deps installed (langchain/langgraph/langchain_aws) and AWS credentials,
since the LLM nodes call Bedrock.

Usage (from langgraph_python/):
    python scripts/smoke_events.py                       # default 3-path sweep
    python scripts/smoke_events.py -q "2024 TFSA limit?" # one custom query
    python scripts/smoke_events.py --user-id user123 -q "I want to contribute $2000"
    python scripts/smoke_events.py --raw                 # dump full event JSON
    ENABLE_THINKING=true BEDROCK_MODEL_ID=anthropic.claude-3-5-sonnet-20240620-v1:0 \
        python scripts/smoke_events.py --thinking        # require a thinking field
"""
import argparse
import json
import logging
import os
import sys

# Allow running from scripts/ — import the graph module from the parent package dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NEW_EVENTS = ("routing_decision", "agent_reasoning", "node_error")

# Default sweep: one query per routing path.
DEFAULT_QUERIES = [
    ("What are the TFSA limits for 2023 and 2024?", None),            # document -> response
    ("What's my contribution room including 2026?", "user123"),       # document -> search
    ("I want to contribute $2000", "user123"),                        # calculation -> transaction
]


class _Capture(logging.Handler):
    """Collects the pure-JSON lines emitted on the 'agent.audit' logger in-process."""

    def __init__(self):
        super().__init__()
        self.events = []

    def emit(self, record):
        try:
            self.events.append(json.loads(record.getMessage()))
        except Exception:
            pass  # non-JSON lines (shouldn't happen on this logger) are ignored


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-q", "--query", action="append", dest="queries",
                        help="Query to run (repeatable). Overrides the default sweep.")
    parser.add_argument("--user-id", default=None,
                        help="user_id to attach (else taken from the default sweep / left unset)")
    parser.add_argument("--raw", action="store_true", help="Print the full event JSON")
    parser.add_argument("--thinking", action="store_true",
                        help="Fail unless at least one llm_call_end carries a 'thinking' field")
    args = parser.parse_args()

    from tfsa_assistant_graph import run_tfsa_assistant_sync

    cap = _Capture()
    logging.getLogger("agent.audit").addHandler(cap)

    if args.queries:
        queries = [(q, args.user_id) for q in args.queries]
    else:
        queries = DEFAULT_QUERIES

    leaked = False
    saw_thinking = False
    counts = {e: 0 for e in NEW_EVENTS}

    for i, (query, user_id) in enumerate(queries, 1):
        prompt = query if not user_id else f"{query}, my user id is {user_id}"
        before = len(cap.events)
        reply, _ = run_tfsa_assistant_sync(prompt, session_id="smoke", message_id=f"m{i}")
        turn_events = cap.events[before:]

        if "REASONING:" in reply or "###ANSWER###" in reply:
            leaked = True

        print(f"\n=== [{i}] {prompt}")
        print(f"--- reply ---\n{reply}\n--- events ---")
        for ev in turn_events:
            et = ev.get("event_type")
            if et in counts:
                counts[et] += 1
            if et == "llm_call_end" and ev.get("thinking"):
                saw_thinking = True
            if args.raw:
                print(json.dumps(ev))
            elif et in NEW_EVENTS:
                detail = ev.get("reason") or ev.get("reasoning") or ev.get("error") or ""
                print(f"  {et:18} node={ev.get('node'):16} "
                      f"{ev.get('decision') or ev.get('data_selected') or ev.get('error_type') or ''} "
                      f"| {detail[:80]}")

    print("\n===== summary =====")
    for e in NEW_EVENTS:
        print(f"  {e:18} {counts[e]}")
    print(f"  reasoning leaked     {'YES (FAIL)' if leaked else 'no'}")
    if args.thinking:
        print(f"  thinking captured    {'yes' if saw_thinking else 'NO (FAIL)'}")

    ok = (counts["routing_decision"] > 0 and counts["agent_reasoning"] > 0
          and not leaked and (saw_thinking or not args.thinking))
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
