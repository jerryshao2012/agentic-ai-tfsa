#!/usr/bin/env python3
"""Pick N random sessions that have NO throttling error from the OTEL agent logs.

Reads the same S3 OTEL log partitions as ``read_otel_logs.py``, groups every audit
event by ``session_id``, marks a session "throttled" if ANY of its lines mentions a
throttling error (default pattern: ``ThrottlingException``), and then randomly samples
N session ids from the remaining clean sessions.

Examples
--------
    # 10 clean sessions from all of Jun 8 2026 (default count = 10)
    python pick_clean_sessions.py --day 8

    # 10 clean sessions, reproducible, dumping each session's lines to ./clean_sessions/
    python pick_clean_sessions.py --day 8 --seed 42 --dump-dir clean_sessions

    # Same, but write ALL chosen sessions into one combined file
    python pick_clean_sessions.py --day 8 --seed 42 --dump-file clean_sessions/all_sessions.jsonl

    # A specific hour range, printing the session ids one per line (for piping)
    python pick_clean_sessions.py --day 8 --start-hour 13 --end-hour 15 --ids-only
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
from collections import defaultdict

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# Reuse the S3 reading + partition helpers from the sibling script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from read_otel_logs import (  # noqa: E402
    DEFAULT_BUCKET, DEFAULT_PREFIX, DEFAULT_REGION,
    build_prefix, iter_objects, read_object_lines,
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    today = dt.date.today()
    p = argparse.ArgumentParser(
        description="Randomly pick N sessions with no throttling error.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--bucket", default=DEFAULT_BUCKET, help=f"S3 bucket (default: {DEFAULT_BUCKET})")
    p.add_argument("--prefix", default=DEFAULT_PREFIX, help=f"Base key prefix (default: {DEFAULT_PREFIX})")
    p.add_argument("--region", default=DEFAULT_REGION, help=f"AWS region (default: {DEFAULT_REGION})")
    p.add_argument("--profile", default=None, help="AWS named profile to use")
    p.add_argument("--year", type=int, default=today.year, help="Partition year (default: today)")
    p.add_argument("--month", type=int, default=today.month, help="Partition month, unpadded (default: today)")
    p.add_argument("--day", type=int, default=today.day, help="Partition day, unpadded (default: today)")
    p.add_argument("--hour", type=int, default=None, help="A single hour 0-23 (overrides start/end-hour)")
    p.add_argument("--start-hour", type=int, default=0, help="First hour 0-23 (default: 0)")
    p.add_argument("--end-hour", type=int, default=23, help="Last hour 0-23 inclusive (default: 23)")
    p.add_argument("--all", action="store_true",
                   help="Scan EVERY partition under the base prefix (ignores year/month/day/hour)")
    p.add_argument("--count", type=int, default=10, help="How many clean sessions to sample (default: 10)")
    p.add_argument("--throttle-pattern", default="ThrottlingException",
                   help="Substring (case-insensitive) that marks a session as throttled "
                        "(default: ThrottlingException)")
    p.add_argument("--seed", type=int, default=None, help="Random seed for a reproducible sample")
    p.add_argument("--ids-only", action="store_true", help="Print only the chosen session ids, one per line")
    p.add_argument("--dump-dir", default=None,
                   help="Write each chosen session's raw lines to <dir>/<session_id>.jsonl")
    p.add_argument("--dump-file", default=None,
                   help="Write ALL chosen sessions' raw lines into a single combined .jsonl file")
    return p.parse_args(argv)


def collect_sessions(s3, args) -> tuple[dict[str, list[str]], set[str]]:
    """Scan the partition and return (lines_by_session, throttled_session_ids).

    A line with no parseable ``session_id`` is ignored (it can't be attributed to a
    session). Throttle detection is a case-insensitive substring match against the raw
    line so it catches the pattern wherever it lands (error field, message content, ...).
    """
    if args.all:
        # Whole-prefix sweep: every year/month/day/hour partition under the base.
        prefixes = [args.prefix.rstrip("/") + "/"]
    elif args.hour is not None:
        prefixes = [build_prefix(args.prefix, args.year, args.month, args.day, args.hour)]
    else:
        prefixes = [build_prefix(args.prefix, args.year, args.month, args.day, h)
                    for h in range(args.start_hour, args.end_hour + 1)]

    pattern = args.throttle_pattern.lower()
    lines_by_session: dict[str, list[str]] = defaultdict(list)
    throttled: set[str] = set()

    for prefix in prefixes:
        for key, _size in iter_objects(s3, args.bucket, prefix):
            for line in read_object_lines(s3, args.bucket, key):
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                sid = rec.get("session_id")
                print(f"Session id - {sid}")
                if not sid:
                    continue
                lines_by_session[sid].append(line)
                if pattern in line.lower():
                    throttled.add(sid)
    return lines_by_session, throttled


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    s3 = session.client("s3")

    try:
        lines_by_session, throttled = collect_sessions(s3, args)
    except NoCredentialsError:
        print("ERROR: no AWS credentials found. Set them via env, --profile, or `aws configure`.", file=sys.stderr)
        return 2
    except ClientError as e:
        print(f"ERROR: S3 request failed: {e}", file=sys.stderr)
        return 2

    all_sessions = set(lines_by_session)
    clean = sorted(all_sessions - throttled)

    if not args.ids_only:
        print(f"Sessions seen:        {len(all_sessions)}", file=sys.stderr)
        print(f"  with throttling:    {len(throttled)}", file=sys.stderr)
        print(f"  clean (no throttle):{len(clean)}", file=sys.stderr)

    if not clean:
        print("No clean sessions found for the given partition.", file=sys.stderr)
        return 1

    if args.seed is not None:
        random.seed(args.seed)
    k = min(args.count, len(clean))
    if k < args.count and not args.ids_only:
        print(f"NOTE: only {len(clean)} clean session(s) available; returning {k}.", file=sys.stderr)
    chosen = random.sample(clean, k)

    if args.dump_dir:
        os.makedirs(args.dump_dir, exist_ok=True)

    combined = None
    if args.dump_file:
        parent = os.path.dirname(args.dump_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        combined = open(args.dump_file, "w", encoding="utf-8")

    try:
        for sid in chosen:
            if args.ids_only:
                print(sid)
            else:
                print(f"{sid}\t({len(lines_by_session[sid])} events)")
            if args.dump_dir:
                out = os.path.join(args.dump_dir, f"{sid}.jsonl")
                with open(out, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines_by_session[sid]) + "\n")
            if combined is not None:
                combined.write("\n".join(lines_by_session[sid]) + "\n")
    finally:
        if combined is not None:
            combined.close()

    if not args.ids_only:
        if args.dump_dir:
            print(f"\nWrote {k} session file(s) to {args.dump_dir}/", file=sys.stderr)
        if args.dump_file:
            total = sum(len(lines_by_session[sid]) for sid in chosen)
            print(f"Wrote {total} events from {k} session(s) to {args.dump_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
