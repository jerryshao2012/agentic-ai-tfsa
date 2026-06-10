#!/usr/bin/env python3
"""Convert agent session ``.jsonl`` trace logs into clean JSON.

Handles both single-session trace files (e.g. ``session_<id>.jsonl``) and large
multi-session otel exports (e.g. ``otel_june10.jsonl`` with thousands of
sessions). For every input file this writes two sibling artifacts:

* ``<name>.full.json``    — a faithful array of every event, all fields kept
                            (lossless reshape of the ``.jsonl``). Streamed to
                            disk, so it works on multi-hundred-MB inputs.
* ``<name>.redteam.json`` — a per-turn, attack-analysis view intended to be fed
                            to an LLM for risk scoring / red-team featurization.
                            Only extracted facts are kept (no fabricated scores).

Per turn the red-team view captures the strongest attack signals:
  * ``detected_intent``  — supervisor router classification (intent drift)
  * ``agents_invoked``   — sub-agent nodes activated (e.g. ``transaction_agent``)
  * ``tools_invoked`` / ``tool_calls`` — actual tools run with args/result/status
                          (e.g. ``execute_tfsa_contribution`` is money-moving)
  * ``data_sources``     — entity/source touched (PII / live-data access)
  * ``reasoning`` / ``routing`` — node reasoning + decision trail
  * ``node_outputs`` / ``llm_calls`` — sub-agent outputs + internal LLM calls
  * ``errors``           — node_error / llm_call_error events
  * ``final_output`` / ``status`` / ``token_usage``

Output shape:
  * one session  -> ``{session_id, turn_count, turns: [...]}``
  * many sessions -> ``{session_count, sessions: [{session_id, ...}, ...]}``

Usage::

    python convert_sessions.py session.jsonl [more.jsonl ...]
    python convert_sessions.py outputs/            # every *.jsonl in the directory
    python convert_sessions.py otel_june10.jsonl --redteam-only
    python convert_sessions.py session.jsonl --stdout   # print redteam, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

# Records carrying these event_types (or none / no session_id) are stray app log
# lines mixed into otel exports, not agent trace events. Kept in full.json for
# losslessness, skipped from the red-team grouping.
TRACE_REQUIRED_FIELDS = ("event_type", "session_id")


def iter_events(path: Path) -> Iterator[dict[str, Any]]:
    """Yield event dicts from a ``.jsonl`` file (blank lines skipped)."""
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc


def _first(events: list[dict[str, Any]], **match: Any) -> dict[str, Any] | None:
    """Return the first event whose fields equal every key in ``match``."""
    for ev in events:
        if all(ev.get(k) == v for k, v in match.items()):
            return ev
    return None


def _pair_by_run(
    turn_events: list[dict[str, Any]], start_type: str, end_type: str
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair ``*_start`` events with their matching ``*_end`` by ``run_id``.

    Yields (start, end) tuples; ``end`` is ``{}`` when unmatched. Orphan end
    events with no start are also yielded as ``({}, end)``.
    """
    ends_by_run = {e.get("run_id"): e for e in turn_events if e.get("event_type") == end_type}
    seen: set[Any] = set()
    for s in turn_events:
        if s.get("event_type") != start_type:
            continue
        rid = s.get("run_id")
        seen.add(rid)
        yield s, ends_by_run.get(rid, {})
    for rid, e in ends_by_run.items():
        if rid not in seen:
            yield {}, e


def _build_turn(turn_events: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse all events sharing a ``message_id`` into one turn record."""
    start = _first(turn_events, event_type="invocation_start") or {}
    end = _first(turn_events, event_type="invocation_end") or {}
    router = _first(turn_events, event_type="agent_reasoning", node="supervisor_router")

    reasoning = [
        {
            "node": e.get("node"),
            "intent": e.get("intent"),
            "reasoning": e.get("reasoning"),
            "needs_search": e.get("needs_search"),
        }
        for e in turn_events
        if e.get("event_type") == "agent_reasoning"
    ]
    routing = [
        {
            "node": e.get("node"),
            "decision": e.get("decision"),
            "reason": e.get("reason"),
            "data_selected": e.get("data_selected"),
        }
        for e in turn_events
        if e.get("event_type") == "routing_decision"
    ]
    node_outputs = [
        {"node": e.get("node"), "content": e.get("content")}
        for e in turn_events
        if e.get("event_type") == "agent_node_output"
    ]
    agents_invoked = sorted({o["node"] for o in node_outputs if o["node"]})

    tool_calls = [
        {
            "tool": s.get("tool") or e.get("tool"),
            "args": s.get("args"),
            "result": e.get("result"),
            "status": e.get("status"),
            "duration_ms": e.get("duration_ms"),
        }
        for s, e in _pair_by_run(turn_events, "tool_call_start", "tool_call")
    ]
    tools_invoked = sorted({tc["tool"] for tc in tool_calls if tc["tool"]})

    data_sources = [
        {"entity": e.get("entity"), "source": e.get("source")}
        for e in turn_events
        if e.get("event_type") == "data_source"
    ]

    llm_calls = [
        {
            "prompt_name": s.get("prompt_name"),
            "prompt_version": s.get("prompt_version"),
            "prompt_role": s.get("prompt_role"),
            "run_name": s.get("run_name"),
            "completion": e.get("completion"),
            "usage": e.get("usage"),
            "duration_ms": e.get("duration_ms"),
        }
        for s, e in _pair_by_run(turn_events, "llm_call_start", "llm_call_end")
    ]

    errors = [
        {
            "event_type": e.get("event_type"),
            "error_type": e.get("error_type"),
            "error": e.get("error"),
            "node": e.get("node"),
        }
        for e in turn_events
        if e.get("event_type") in ("node_error", "llm_call_error")
    ]

    return {
        "message_id": turn_events[0].get("message_id"),
        "start_ts": start.get("ts"),
        "user_input": start.get("input"),
        "detected_intent": router.get("intent") if router else None,
        "agents_invoked": agents_invoked,
        "tools_invoked": tools_invoked,
        "reasoning": reasoning,
        "routing": routing,
        "data_sources": data_sources,
        "node_outputs": node_outputs,
        "tool_calls": tool_calls,
        "llm_calls": llm_calls,
        "errors": errors,
        "final_output": end.get("output"),
        "status": end.get("status"),
        "duration_ms": end.get("duration_ms"),
        "token_usage": end.get("token_usage"),
    }


def _turn_sort_key(turn: dict[str, Any]) -> tuple[bool, str]:
    return (turn["start_ts"] is None, turn["start_ts"] or "")


def _build_session(session_id: Any, evs: list[dict[str, Any]]) -> dict[str, Any]:
    turns_by_msg: dict[Any, list[dict[str, Any]]] = {}
    for ev in evs:
        turns_by_msg.setdefault(ev.get("message_id"), []).append(ev)
    turns = sorted((_build_turn(t) for t in turns_by_msg.values()), key=_turn_sort_key)
    original = next((e.get("original_session_id") for e in evs if e.get("original_session_id")), None)
    session = {"session_id": session_id}
    if original:
        session["original_session_id"] = original
    session.update({"turn_count": len(turns), "turns": turns})
    return session


def build_redteam(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Group events into the red-team analysis view (single- or multi-session)."""
    by_session: dict[Any, list[dict[str, Any]]] = {}
    for ev in events:
        if not all(ev.get(f) for f in TRACE_REQUIRED_FIELDS):
            continue  # stray app log line, not an agent trace event
        by_session.setdefault(ev["session_id"], []).append(ev)

    sessions = [_build_session(sid, evs) for sid, evs in by_session.items()]
    sessions.sort(key=lambda s: _turn_sort_key(s["turns"][0]) if s["turns"] else (True, ""))

    if len(sessions) == 1:
        s = sessions[0]
        out = {"session_id": s["session_id"]}
        if "original_session_id" in s:
            out["original_session_id"] = s["original_session_id"]
        out.update({"turn_count": s["turn_count"], "turns": s["turns"]})
        return out
    return {"session_count": len(sessions), "sessions": sessions}


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_full_streaming(path: Path, events: Iterator[dict[str, Any]]) -> int:
    """Write a pretty-printed JSON array of events one at a time (bounded memory).

    Byte-identical to ``json.dumps(list(events), indent=2)`` but never holds the
    whole serialized string, so it scales to multi-hundred-MB inputs.
    """
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write("[\n" if count == 0 else ",\n")
            chunk = json.dumps(ev, indent=2, ensure_ascii=False)
            fh.write("  " + chunk.replace("\n", "\n  "))
            count += 1
        fh.write("\n]\n" if count else "[]\n")
    return count


def convert(path: Path, full: bool, redteam: bool, to_stdout: bool) -> dict[str, Any] | None:
    if to_stdout:
        rt = build_redteam(list(iter_events(path)))
        json.dump(rt, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return rt

    if full:
        out = path.with_suffix(".full.json")
        n = _write_full_streaming(out, iter_events(path))
        print(f"  wrote {out.name}  ({n} events)")
    if redteam:
        rt = build_redteam(list(iter_events(path)))
        out = path.with_suffix(".redteam.json")
        _write_json(out, rt)
        if "sessions" in rt:
            print(f"  wrote {out.name}  ({rt['session_count']} sessions)")
        else:
            print(f"  wrote {out.name}  ({rt['turn_count']} turns)")
        return rt
    return None


def _resolve_inputs(args_inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in args_inputs:
        p = Path(raw)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.jsonl")))
        else:
            paths.append(p)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("inputs", nargs="+", help="session .jsonl file(s) or a directory of them")
    parser.add_argument("--full-only", action="store_true", help="write only <name>.full.json")
    parser.add_argument("--redteam-only", action="store_true", help="write only <name>.redteam.json")
    parser.add_argument("--stdout", action="store_true", help="print redteam JSON to stdout, write no files")
    args = parser.parse_args(argv)

    full = not args.redteam_only
    redteam = not args.full_only

    paths = _resolve_inputs(args.inputs)
    if not paths:
        parser.error("no .jsonl inputs found")

    rc = 0
    for path in paths:
        if not path.exists():
            print(f"skip (not found): {path}", file=sys.stderr)
            rc = 1
            continue
        if not args.stdout:
            print(f"{path}:")
        try:
            convert(path, full=full, redteam=redteam, to_stdout=args.stdout)
        except ValueError as exc:
            print(f"  error: {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
