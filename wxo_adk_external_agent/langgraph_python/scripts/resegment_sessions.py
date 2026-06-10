#!/usr/bin/env python3
"""Re-segment colliding ``session_id``s back into individual conversations.

The OTEL dumps were keyed by ``session_id``, but many distinct multi-turn
conversations ended up sharing a single ``session_id`` (and no other id survives
to tell them apart: ``thread_id`` is always null, ``trace_id``/``message_id``/
``invocation_id`` are per-turn UUIDs, ``user_id`` is mostly "unknown").

The one reliable signal is time: turns within a real conversation arrive seconds
apart, whereas separate conversations are minutes-to-hours apart. This script
groups each ``session_id``'s records into turns (by ``message_id``), orders them,
and starts a new conversation whenever the gap to the previous turn exceeds
``--gap-seconds`` (default 180s, which sits in the natural valley of the gap
distribution).

Each record gets its original id copied to ``original_session_id`` and its
``session_id`` overwritten with a new per-conversation id ``{orig}__c{NN}``.
Records with no ``session_id`` (stray runtime log lines) pass through untouched.
The source file is never modified.

Example
-------
    python resegment_sessions.py \
        --input ../outputs/otel_data.jsonl \
        --output ../outputs/otel_data.resegmented.jsonl \
        --gap-seconds 180
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import sys

try:
    # Reuse the shared readers when their cloud deps (boto3) are available.
    from otel_logs import iter_local_lines, open_output
except ModuleNotFoundError:
    # Local-only fallback: otel_logs pulls in boto3 at import time, which isn't
    # needed here. Mirror the two helpers we use so the repair runs offline.
    def iter_local_lines(paths):
        for path in paths:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        yield line.rstrip("\n")

    def open_output(path):
        if not path:
            return sys.stdout
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return open(path, "w", encoding="utf-8")


def parse_ts(value) -> dt.datetime | None:
    """Parse an ISO-8601 ``ts`` string, or return None if absent/unparseable."""
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def build_remap(records: list[dict], gap_seconds: float) -> dict[int, str]:
    """Map record identity -> new session id, segmenting each session by time gaps.

    Records are grouped per ``session_id`` and then per turn (``message_id``);
    a turn carries the minimum ``ts`` of its records. Turns are ordered by time
    and split into conversations wherever the inter-turn gap exceeds ``gap_seconds``.
    Returns ``{id(record): new_session_id}`` for every record that has a session_id.
    """
    # session_id -> turn_key -> list[record]
    by_session = collections.defaultdict(lambda: collections.defaultdict(list))
    for rec in records:
        sid = rec.get("session_id")
        if not sid:
            continue
        # Fall back to a per-record key when message_id is missing so the record
        # still forms its own (singleton) turn rather than being dropped.
        turn_key = rec.get("message_id") or f"_norec_{id(rec)}"
        by_session[sid][turn_key].append(rec)

    remap: dict[int, str] = {}
    for sid in sorted(by_session):
        turns = by_session[sid]
        # (turn_min_ts, turn_key, records); ts-less turns sort to the end.
        ordered = []
        for turn_key, recs in turns.items():
            tss = [parse_ts(r.get("ts")) for r in recs]
            tss = [t for t in tss if t is not None]
            ordered.append((min(tss) if tss else dt.datetime.max, turn_key, recs))
        ordered.sort(key=lambda t: t[0])

        conv_idx = 0
        prev_ts: dt.datetime | None = None
        for turn_ts, _turn_key, recs in ordered:
            if prev_ts is None:
                conv_idx = 1
            elif turn_ts is dt.datetime.max or prev_ts is dt.datetime.max:
                conv_idx += 1  # missing timestamps: be conservative, split.
            elif (turn_ts - prev_ts).total_seconds() > gap_seconds:
                conv_idx += 1
            prev_ts = turn_ts
            new_sid = f"session_{sid}__c{conv_idx:02d}"
            for r in recs:
                remap[id(r)] = new_sid
    return remap


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--input", required=True, help="Source .jsonl dump (read-only)")
    p.add_argument("--output", required=True, help="Repaired .jsonl to write")
    p.add_argument("--gap-seconds", type=float, default=180.0,
                   help="Inter-turn gap (s) above which a new conversation starts "
                        "(default: 180; natural valley is 60-300)")
    args = p.parse_args(argv)

    # Read everything once, preserving original line order and raw passthroughs.
    # entries: list of (raw_line, parsed_or_None)
    entries: list[tuple[str, dict | None]] = []
    parsed: list[dict] = []
    for line in iter_local_lines([args.input]):
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            entries.append((line, None))
            continue
        if not isinstance(rec, dict):
            entries.append((line, None))
            continue
        entries.append((line, rec))
        parsed.append(rec)

    remap = build_remap(parsed, args.gap_seconds)

    out = open_output(args.output)
    written = passthrough = rekeyed = 0
    new_ids: set[str] = set()
    try:
        for raw, rec in entries:
            if rec is None:
                out.write(raw + "\n")
                written += 1
                passthrough += 1
                continue
            new_sid = remap.get(id(rec))
            if new_sid is None:
                # parsed but no session_id -> leave untouched
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written += 1
                passthrough += 1
                continue
            rec["original_session_id"] = rec.get("session_id")
            rec["session_id"] = new_sid
            new_ids.add(new_sid)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
            rekeyed += 1
    finally:
        if out is not sys.stdout:
            out.close()

    orig_sessions = {r.get("original_session_id") for r in parsed
                     if r.get("original_session_id")}
    # Turn-length histogram (turns per reconstructed conversation).
    turns_per_conv: dict[str, set] = collections.defaultdict(set)
    for r in parsed:
        sid = r.get("session_id")
        if sid in new_ids:
            turns_per_conv[sid].add(r.get("message_id"))
    hist = collections.Counter(len(v) for v in turns_per_conv.values())

    w = sys.stderr.write
    w(f"\nRead {len(entries)} line(s); wrote {written}.\n")
    w(f"  re-keyed records:   {rekeyed}\n")
    w(f"  passed through:     {passthrough}\n")
    w(f"  session_ids in:     {len(orig_sessions)}\n")
    w(f"  conversations out:  {len(new_ids)}\n")
    w(f"  turns/conversation histogram (len: count):\n")
    for length in sorted(hist):
        w(f"     {length:3}: {hist[length]}\n")
    w(f"\nWrote {args.output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
