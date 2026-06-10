#!/usr/bin/env python3
"""Read OTEL agent logs out of S3.

The exporter lays logs down under a Hive-style partition layout:

    s3://agent-otel-logs-668864905269-us-east-1-tfsa-agent/
        agent-otel-logs/
            year=2026/month=6/day=8/hour=00/  ... hour=23/
                <object>            # gzipped or plain JSON / JSON-lines

This script lists the objects under a given day (and optional hour range),
downloads them, transparently gunzips, and prints each record. Records are
parsed as JSON-lines when possible and can be filtered with --grep (a plain
substring match against the raw line) or pretty-printed with --pretty.

Examples
--------
    # Everything for Jun 8 2026
    python read_otel_logs.py --day 8

    # Just hours 13-15, only lines mentioning an error, as pretty JSON
    python read_otel_logs.py --day 8 --start-hour 13 --end-hour 15 \
        --grep error --pretty

    # A datetime range that spans days, saved to a file (hourly granularity)
    python read_otel_logs.py --start "2026-06-08 13:00" --end "2026-06-09 05:00" \
        --output otel_jun8-9.jsonl

    # A different month/year and just list the object keys (no download)
    python read_otel_logs.py --year 2026 --month 6 --day 7 --list-only
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import sys

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

DEFAULT_BUCKET = "agent-otel-logs-668864905269-us-east-1-tfsa-agent"
DEFAULT_PREFIX = "agent-otel-logs"
DEFAULT_REGION = "us-east-1"


def build_prefix(base: str, year: int, month: int, day: int, hour: int | None) -> str:
    """Build the partition prefix. month/day/hour are zero-padded to two
    digits to match the exporter layout (month=06, day=07, hour=01)."""
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
    """Yield (year, month, day, hour) for every hour partition in [start, end].

    Partitions are hourly, so minutes only affect which end hours are included
    (e.g. end=05:00 still includes the whole hour=05 partition)."""
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


def read_object_lines(s3, bucket: str, key: str):
    """Download an object, gunzip if needed, and yield decoded text lines."""
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if body[:2] == b"\x1f\x8b":  # gzip magic number
        try:
            body = gzip.decompress(body)
        except OSError:
            pass  # not actually gzip / truncated — fall through to raw
    text = body.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.strip():
            yield line


def emit(line: str, pretty: bool, out) -> None:
    if pretty:
        try:
            line = json.dumps(json.loads(line), indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            pass  # not JSON — write verbatim
    out.write(line + "\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    today = dt.date.today()
    p = argparse.ArgumentParser(
        description="Read OTEL agent logs from S3.",
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
    p.add_argument("--start", type=parse_dt, default=None,
                   help="Start datetime, e.g. '2026-06-08 13:00'. Spans days; overrides year/month/day/hour")
    p.add_argument("--end", type=parse_dt, default=None,
                   help="End datetime (inclusive, hourly). Defaults to end of --start's day")
    p.add_argument("--grep", default=None, help="Only print lines containing this substring")
    p.add_argument("--pretty", action="store_true", help="Pretty-print lines that parse as JSON")
    p.add_argument("--list-only", action="store_true", help="List matching object keys, don't download")
    p.add_argument("--limit", type=int, default=None, help="Stop after printing this many lines")
    p.add_argument("-o", "--output", default=None,
                   help="Write records to this file instead of stdout (status messages still go to stderr)")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    # Build the list of (year, month, day, hour) partitions to scan.
    if args.start is not None:
        end = args.end or args.start.replace(hour=23, minute=0)
        partitions = list(iter_partitions(args.start, end))
    elif args.hour is not None:
        partitions = [(args.year, args.month, args.day, args.hour)]
    else:
        partitions = [(args.year, args.month, args.day, h)
                      for h in range(args.start_hour, args.end_hour + 1)]

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    s3 = session.client("s3")

    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout

    printed = 0
    matched_objects = 0
    try:
        for year, month, day, hour in partitions:
            prefix = build_prefix(args.prefix, year, month, day, hour)
            for key, size in iter_objects(s3, args.bucket, prefix):
                matched_objects += 1
                if args.list_only:
                    print(f"{key}\t{size}")
                    continue
                for line in read_object_lines(s3, args.bucket, key):
                    if args.grep and args.grep not in line:
                        continue
                    emit(line, args.pretty, out)
                    printed += 1
                    if args.limit is not None and printed >= args.limit:
                        print(f"\n[reached --limit {args.limit}]", file=sys.stderr)
                        return 0
    except NoCredentialsError:
        print("ERROR: no AWS credentials found. Set them via env, --profile, or `aws configure`.", file=sys.stderr)
        return 2
    except ClientError as e:
        print(f"ERROR: S3 request failed: {e}", file=sys.stderr)
        return 2
    finally:
        if out is not sys.stdout:
            out.close()

    if matched_objects == 0:
        print("No objects found for the given partition.", file=sys.stderr)
    elif not args.list_only:
        dest = f" to {args.output}" if args.output else ""
        print(f"\n[{printed} line(s) from {matched_objects} object(s){dest}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
