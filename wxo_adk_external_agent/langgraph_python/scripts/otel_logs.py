#!/usr/bin/env python3
"""Read OTEL agent logs from S3 (or local dumps) and pick clean sessions.

Two subcommands share one source layer:

    read   list / print / grep raw log records
    pick   sample N sessions that have NO throttling error

Source: by default the Hive-partitioned S3 layout

    s3://<bucket>/<prefix>/year=2026/month=06/day=08/hour=00..23/<object>

or, with ``--input``, local .jsonl file(s)/dir(s) already fetched via ``read -o``.

Examples
--------
    # Dump everything for Jun 8 2026 to a local file
    python otel_logs.py read --day 8 -o otel_jun8.jsonl

    # Just hours 13-15, error lines only, pretty JSON
    python otel_logs.py read --day 8 --start-hour 13 --end-hour 15 --grep error --pretty

    # 10 clean sessions from that local dump, reproducible, one file per session
    python otel_logs.py pick --input otel_jun8.jsonl --seed 42 --dump-dir clean_sessions

    # Clean session ids straight from S3 for a single hour, one per line
    python otel_logs.py pick --day 8 --hour 14 --ids-only
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import gzip
import json
import os
import random
import sys
import time
from collections import defaultdict

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionError as BotoConnectionError,
    IncompleteReadError,
    NoCredentialsError,
    ReadTimeoutError,
    ResponseStreamingError,
)

DEFAULT_BUCKET = "agent-otel-logs-668864905269-us-east-1-tfsa-agent"
DEFAULT_PREFIX = "agent-otel-logs"
DEFAULT_REGION = "us-east-1"

# Transient errors worth retrying when streaming an object body. These come from
# dropped/flaky connections mid-download, not from the request itself, so botocore's
# built-in request retries don't cover them.
_RETRYABLE = (
    ResponseStreamingError,
    IncompleteReadError,
    ReadTimeoutError,
    BotoConnectionError,
)
_MAX_RETRIES = 5


# --------------------------------------------------------------------------- source

def build_prefix(base: str, year: int, month: int, day: int, hour: int | None) -> str:
    """Build a partition prefix; month/day/hour are zero-padded (month=06, hour=01)."""
    parts = [base.rstrip("/"), f"year={year}", f"month={month:02d}", f"day={day:02d}"]
    if hour is not None:
        parts.append(f"hour={hour:02d}")
    return "/".join(parts) + "/"


_DT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H",
    "%Y-%m-%dT%H",
    "%Y-%m-%d",
)


def parse_dt(s: str) -> dt.datetime:
    """Parse a start/end argument like '2026-06-08 13:00' or '2026-06-08'."""
    for fmt in _DT_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"unrecognized datetime {s!r}; use e.g. '2026-06-08 13:00' or '2026-06-08'"
    )


def iter_partitions(start: dt.datetime, end: dt.datetime):
    """Yield (year, month, day, hour) for every hour partition in [start, end]."""
    cur = start.replace(minute=0, second=0, microsecond=0)
    last = end.replace(minute=0, second=0, microsecond=0)
    while cur <= last:
        yield cur.year, cur.month, cur.day, cur.hour
        cur += dt.timedelta(hours=1)


def iter_objects(s3, bucket: str, prefix: str):
    """Yield (key, size) for every object under prefix, paginating fully."""
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            yield obj["Key"], obj["Size"]


def download_object(s3, bucket: str, key: str) -> bytes:
    """Fetch an object's full body, retrying transient streaming/connection drops."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except _RETRYABLE as e:
            if attempt == _MAX_RETRIES:
                raise
            backoff = min(2 ** (attempt - 1), 10)
            print(f"WARN: read failed for {key} (attempt {attempt}/{_MAX_RETRIES}): {e}; "
                  f"retrying in {backoff}s", file=sys.stderr)
            time.sleep(backoff)
    raise AssertionError("unreachable")  # loop either returns or raises


def read_object_lines(s3, bucket: str, key: str):
    """Download an object, gunzip if needed, and yield non-empty text lines."""
    body = download_object(s3, bucket, key)
    if body[:2] == b"\x1f\x8b":  # gzip magic number
        try:
            body = gzip.decompress(body)
        except OSError:
            pass  # not actually gzip / truncated — fall through to raw
    text = body.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.strip():
            yield line


def expand_inputs(paths: list[str]) -> list[str]:
    """Resolve --input paths into a flat list of files (dirs expand to their *.jsonl)."""
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "*.jsonl"))))
        else:
            files.append(p)
    return files


def iter_local_lines(paths: list[str]):
    """Yield non-empty lines from local .jsonl file(s)/dir(s)."""
    for path in expand_inputs(paths):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield line.rstrip("\n")


def source_prefixes(args: argparse.Namespace) -> list[str]:
    """Translate the partition-selection args into S3 prefixes to scan."""
    if args.all:
        return [args.prefix.rstrip("/") + "/"]
    if args.start is not None:
        end = args.end or args.start.replace(hour=23, minute=0)
        return [build_prefix(args.prefix, y, m, d, h) for y, m, d, h in iter_partitions(args.start, end)]
    if args.hour is not None:
        return [build_prefix(args.prefix, args.year, args.month, args.day, args.hour)]
    return [build_prefix(args.prefix, args.year, args.month, args.day, h)
            for h in range(args.start_hour, args.end_hour + 1)]


def make_s3(args: argparse.Namespace):
    cfg = Config(
        retries={"max_attempts": _MAX_RETRIES, "mode": "adaptive"},
        connect_timeout=30,
        read_timeout=120,
    )
    return boto3.Session(profile_name=args.profile, region_name=args.region).client("s3", config=cfg)


def iter_source_lines(args: argparse.Namespace):
    """Yield raw log lines from local --input file(s) or, otherwise, from S3."""
    if args.input:
        yield from iter_local_lines(args.input)
        return
    s3 = make_s3(args)
    for prefix in source_prefixes(args):
        for key, _size in iter_objects(s3, args.bucket, prefix):
            yield from read_object_lines(s3, args.bucket, key)


def open_output(path: str | None):
    """Open an output file (creating parent dirs) or fall back to stdout."""
    if not path:
        return sys.stdout
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return open(path, "w", encoding="utf-8")


# ----------------------------------------------------------------------------- read

def emit(line: str, pretty: bool, out) -> None:
    if pretty:
        try:
            line = json.dumps(json.loads(line), indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            pass  # not JSON — write verbatim
    out.write(line + "\n")


def cmd_read(args: argparse.Namespace) -> int:
    if args.list_only:
        if args.input:
            print("ERROR: --list-only lists S3 keys and is not valid with --input.", file=sys.stderr)
            return 2
        s3 = make_s3(args)
        found = 0
        for prefix in source_prefixes(args):
            for key, size in iter_objects(s3, args.bucket, prefix):
                print(f"{key}\t{size}")
                found += 1
        if found == 0:
            print("No objects found for the given partition.", file=sys.stderr)
        return 0

    out = open_output(args.output)
    printed = 0
    try:
        for line in iter_source_lines(args):
            if args.grep and args.grep not in line:
                continue
            emit(line, args.pretty, out)
            printed += 1
            if args.limit is not None and printed >= args.limit:
                print(f"\n[reached --limit {args.limit}]", file=sys.stderr)
                return 0
    finally:
        if out is not sys.stdout:
            out.close()

    if printed == 0:
        print("No matching records found.", file=sys.stderr)
    else:
        dest = f" to {args.output}" if args.output else ""
        print(f"\n[{printed} line(s){dest}]", file=sys.stderr)
    return 0


# ----------------------------------------------------------------------------- pick

def collect_sessions(
    args: argparse.Namespace,
) -> tuple[dict[str, list[str]], dict[str, set[str]], set[str]]:
    """Group lines by ``session_id`` and flag sessions with any throttling error.

    Lines with no parseable ``session_id`` are ignored. Throttle detection is a
    case-insensitive substring match against the raw line, so it catches the pattern
    wherever it lands (error field, message content, ...). Alongside the raw lines,
    each session's set of distinct ``message_id`` values is tracked so callers can
    report unique messages rather than raw record counts.
    """
    pattern = args.throttle_pattern.lower()
    lines_by_session: dict[str, list[str]] = defaultdict(list)
    msg_ids_by_session: dict[str, set[str]] = defaultdict(set)
    throttled: set[str] = set()

    for line in iter_source_lines(args):
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        sid = rec.get("session_id")
        if not sid:
            continue
        lines_by_session[sid].append(line)
        mid = rec.get("message_id")
        if mid:
            msg_ids_by_session[sid].add(mid)
        if pattern in line.lower():
            throttled.add(sid)
    return lines_by_session, msg_ids_by_session, throttled


def cmd_pick(args: argparse.Namespace) -> int:
    lines_by_session, msg_ids_by_session, throttled = collect_sessions(args)
    all_sessions = set(lines_by_session)
    clean = sorted(all_sessions - throttled)

    if not args.ids_only:
        print(f"Sessions seen:        {len(all_sessions)}", file=sys.stderr)
        print(f"  with throttling:    {len(throttled)}", file=sys.stderr)
        print(f"  clean (no throttle):{len(clean)}", file=sys.stderr)

    if not clean:
        print("No clean sessions found for the given source.", file=sys.stderr)
        return 1

    if args.seed is not None:
        random.seed(args.seed)
    k = min(args.count, len(clean))
    if k < args.count and not args.ids_only:
        print(f"NOTE: only {len(clean)} clean session(s) available; returning {k}.", file=sys.stderr)
    chosen = random.sample(clean, k)

    if args.dump_dir:
        os.makedirs(args.dump_dir, exist_ok=True)
    combined = open_output(args.dump_file) if args.dump_file else None
    out = open_output(args.output)
    try:
        for sid in chosen:
            out.write((sid if args.ids_only else f"{sid}\t({len(msg_ids_by_session[sid])} messages)") + "\n")
            blob = "\n".join(lines_by_session[sid]) + "\n"
            if args.dump_dir:
                with open(os.path.join(args.dump_dir, f"{sid}.jsonl"), "w", encoding="utf-8") as fh:
                    fh.write(blob)
            if combined is not None:
                combined.write(blob)
    finally:
        if combined is not None:
            combined.close()
        if out is not sys.stdout:
            out.close()

    if not args.ids_only:
        if args.dump_dir:
            print(f"\nWrote {k} session file(s) to {args.dump_dir}/", file=sys.stderr)
        if args.dump_file:
            total = sum(len(lines_by_session[sid]) for sid in chosen)
            messages = sum(len(msg_ids_by_session[sid]) for sid in chosen)
            print(f"Wrote {total} records ({messages} messages) from {k} session(s) to {args.dump_file}",
                  file=sys.stderr)
    return 0


# ------------------------------------------------------------------------------ cli

def add_source_args(p: argparse.ArgumentParser) -> None:
    """Add the source-selection args shared by both subcommands."""
    today = dt.date.today()
    p.add_argument("--input", nargs="+", default=None, metavar="PATH",
                   help="Read local .jsonl file(s)/dir(s) instead of S3 (skips all partition args)")
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
    p.add_argument("--start", type=parse_dt, default=None,
                   help="Start datetime, e.g. '2026-06-08 13:00'. Spans days; overrides year/month/day/hour")
    p.add_argument("--end", type=parse_dt, default=None,
                   help="End datetime (inclusive, hourly). Defaults to end of --start's day")
    p.add_argument("--all", action="store_true",
                   help="Scan EVERY partition under the base prefix (ignores year/month/day/hour)")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Read OTEL agent logs and pick clean sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("read", help="print/grep raw log records",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_source_args(pr)
    pr.add_argument("--grep", default=None, help="Only print lines containing this substring")
    pr.add_argument("--pretty", action="store_true", help="Pretty-print lines that parse as JSON")
    pr.add_argument("--list-only", action="store_true", help="List matching S3 object keys, don't download")
    pr.add_argument("--limit", type=int, default=None, help="Stop after printing this many lines")
    pr.add_argument("-o", "--output", default=None,
                    help="Write records to this file instead of stdout (status still goes to stderr)")
    pr.set_defaults(func=cmd_read)

    pp = sub.add_parser("pick", help="sample N sessions with no throttling error",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_source_args(pp)
    pp.add_argument("--count", type=int, default=10, help="How many clean sessions to sample (default: 10)")
    pp.add_argument("--throttle-pattern", default="ThrottlingException",
                    help="Case-insensitive substring marking a session as throttled "
                         "(default: ThrottlingException)")
    pp.add_argument("--seed", type=int, default=None, help="Random seed for a reproducible sample")
    pp.add_argument("--ids-only", action="store_true", help="Output only the chosen session ids, one per line")
    pp.add_argument("--dump-dir", default=None,
                    help="Write each chosen session's raw lines to <dir>/<session_id>.jsonl")
    pp.add_argument("--dump-file", default=None,
                    help="Write ALL chosen sessions' raw lines into a single combined .jsonl file")
    pp.add_argument("-o", "--output", default=None,
                    help="Write the chosen-session list to this file instead of stdout")
    pp.set_defaults(func=cmd_pick)

    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        return args.func(args)
    except NoCredentialsError:
        print("ERROR: no AWS credentials found. Set them via env, --profile, or `aws configure`.", file=sys.stderr)
        return 2
    except ClientError as e:
        print(f"ERROR: S3 request failed: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
