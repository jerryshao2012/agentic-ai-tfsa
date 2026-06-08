# data_sources.py
"""
Data-source layer for the TFSA agent.

Loads user profiles, TFSA reference limits, and transaction history from S3 when a
bucket is configured (see config.DATA_S3_BUCKET), with a graceful fallback to the
built-in mock / local file so the agent keeps working in local dev without S3.

Layout in S3 (one JSON object per user; keyed by user_id):
    s3://{DATA_S3_BUCKET}/{PROFILE_S3_PREFIX}/{user_id}.json        -> profile dict
    s3://{DATA_S3_BUCKET}/{TRANSACTIONS_S3_PREFIX}/{user_id}.json   -> list[transaction]
    s3://{DATA_S3_BUCKET}/{LIMITS_S3_KEY}                           -> {"2009": 5000, ...}

All loaders are best-effort and never raise: on any S3/parse error they log a warning
and return the fallback, so a data-source outage degrades gracefully rather than
breaking an invocation.
"""
import json
import logging
import os
import threading
from typing import Optional

import config

# Built-in mock profile (the historical hardcoded "Melanie" record). Used as the
# fallback when no S3 bucket is configured or the object is missing.
_DEFAULT_PROFILE = {
    "name": "Melanie",
    "age": 25,
    "residency_status": "Canadian Resident",
    "sin": "123-456-789",
    "first_tfsa_year": 2023,
    "past_contributions": 6500,
    "withdrawals_last_year": 2000,
    "current_year_contributions": 1500,
    "checking_balance": 8500.00,
}

# Simple in-process cache so repeated lookups within a container don't re-hit S3.
_cache: dict[str, object] = {}
_cache_lock = threading.Lock()

_s3_client = None
_s3_lock = threading.Lock()


def _s3():
    """Lazily build (and memoize) a boto3 S3 client; None if boto3 is unavailable."""
    global _s3_client
    if _s3_client is None:
        with _s3_lock:
            if _s3_client is None:
                try:
                    import boto3
                    _s3_client = boto3.client("s3", region_name=config.DATA_S3_REGION)
                except Exception as e:  # boto3 missing or no creds at import time
                    logging.warning("S3 client unavailable, using local fallbacks: %s", e)
                    _s3_client = False  # sentinel: tried and failed
    return _s3_client or None


def _get_json(key: str):
    """GetObject + json.loads for a key in DATA_S3_BUCKET. Returns None on any miss/error."""
    bucket = config.DATA_S3_BUCKET
    if not bucket:
        return None
    client = _s3()
    if client is None:
        return None
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())
    except client.exceptions.NoSuchKey:  # type: ignore[union-attr]
        logging.info("S3 object not found: s3://%s/%s", bucket, key)
        return None
    except Exception as e:
        logging.warning("Failed to load s3://%s/%s: %s", bucket, key, e)
        return None


def load_user_profile(user_id: str) -> dict:
    """Return the profile for user_id from S3, or the built-in mock as fallback.

    Always returns a dict that includes user_id so downstream code (which reads
    profile["user_id"]) is unaffected.
    """
    cache_key = f"profile:{user_id}"
    with _cache_lock:
        if cache_key in _cache:
            return dict(_cache[cache_key])  # copy so callers can't mutate the cache

    key = f"{config.PROFILE_S3_PREFIX}/{user_id}.json"
    data = _get_json(key)
    if not isinstance(data, dict):
        # Fallback to the built-in mock, stamped with the requested user_id.
        # `_source` lets callers log/expose whether real S3 data or the mock was served.
        data = {"user_id": user_id, "_source": "mock", **_DEFAULT_PROFILE}
    else:
        data.setdefault("user_id", user_id)
        data["_source"] = "s3"

    with _cache_lock:
        _cache[cache_key] = dict(data)
    return data


def load_tfsa_limits() -> dict:
    """Return TFSA annual limits {int_year: int_limit} from S3, else local file, else {}.

    Mirrors the on-disk tfsa_limits.json shape (string year keys) and normalizes keys
    to ints for the caller.
    """
    cache_key = "tfsa_limits"
    with _cache_lock:
        if cache_key in _cache:
            return dict(_cache[cache_key])

    raw = _get_json(config.LIMITS_S3_KEY)
    if not isinstance(raw, dict):
        # Local file fallback (the file baked into the image).
        try:
            local = os.path.join(os.path.dirname(__file__), "tfsa_limits.json")
            if os.path.exists(local):
                with open(local, "r") as f:
                    raw = json.load(f)
        except Exception as e:
            logging.warning("Failed to load local tfsa_limits.json: %s", e)
            raw = None

    limits: dict[int, int] = {}
    if isinstance(raw, dict):
        for year, limit in raw.items():
            try:
                limits[int(year)] = int(limit)
            except (ValueError, TypeError):
                continue

    with _cache_lock:
        _cache[cache_key] = dict(limits)
    return limits


def load_user_transactions(user_id: str) -> list:
    """Return the synthetic transaction history for user_id from S3, or [] as fallback."""
    cache_key = f"txns:{user_id}"
    with _cache_lock:
        if cache_key in _cache:
            return list(_cache[cache_key])

    key = f"{config.TRANSACTIONS_S3_PREFIX}/{user_id}.json"
    data = _get_json(key)
    if not isinstance(data, list):
        data = []

    with _cache_lock:
        _cache[cache_key] = list(data)
    return data


def clear_cache() -> None:
    """Drop the in-process cache (useful in tests or after data refresh)."""
    with _cache_lock:
        _cache.clear()
