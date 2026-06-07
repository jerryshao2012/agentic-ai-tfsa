#!/usr/bin/env python3
"""
Generate synthetic TFSA data and upload it to S3 in the layout the agent expects.

Produces, for N users:
  s3://{bucket}/{profile_prefix}/{user_id}.json        - profile dict
  s3://{bucket}/{txn_prefix}/{user_id}.json            - list of contribution/withdrawal txns
  s3://{bucket}/{limits_key}                            - {"2009": 5000, ...}

The agent reads these via data_sources.py when DATA_S3_BUCKET is set. This script is the
reproducible source of the demo dataset — re-run it to reset/extend the data.

Usage:
  python scripts/upload_synthetic_data.py --bucket my-bucket --count 25
  python scripts/upload_synthetic_data.py --bucket my-bucket --dry-run      # print, don't upload

Defaults for prefixes/keys/region come from config.py (env-overridable). Requires boto3 and
credentials with s3:PutObject on the bucket.
"""
import argparse
import json
import os
import random
import sys

# Allow running from anywhere: import config/data_sources from the package dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

# Canonical TFSA annual limits (the reference dataset).
TFSA_LIMITS = {
    "2009": 5000, "2010": 5000, "2011": 5000, "2012": 5000,
    "2013": 5500, "2014": 5500, "2015": 10000, "2016": 5500,
    "2017": 5500, "2018": 5500, "2019": 6000, "2020": 6000,
    "2021": 6000, "2022": 6000, "2023": 6500, "2024": 7000,
    "2025": 7000, "2026": 7000,
}

FIRST_NAMES = ["Melanie", "Arjun", "Sofia", "Liam", "Noah", "Priya", "Wei", "Fatima",
               "Diego", "Chloe", "Omar", "Hana", "Lucas", "Ava", "Mateo", "Yuki",
               "David", "Ravi", "Elena", "Tariq", "Mia", "Sven", "Aisha", "Carlos",
               "Ingrid", "Hannah", "Leila", "Pierre", "Sara", "Tomas", "Nadia", "Kenji",
               "Grace", "Hassan", "Olivia", "Dimitri"]
LAST_NAMES = ["Tremblay", "Smith", "Patel", "Nguyen", "Garcia", "Khan", "Wang", "Okafor",
              "Brown", "Singh", "Rossi", "Kim", "Dubois", "Silva", "Cohen", "Ali",
              "Andersson", "Lopez", "Mueller", "Hassan", "Chen", "Martin", "Reyes", "Ivanov"]
RESIDENCY = ["Canadian Resident", "Non-Resident"]


def _fake_sin() -> str:
    return f"{random.randint(100,999)}-{random.randint(100,999)}-{random.randint(100,999)}"


def make_profile(user_id: str) -> dict:
    first_year = random.choice([2019, 2020, 2021, 2022, 2023])
    return {
        "user_id": user_id,
        "name": f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}",
        "age": random.randint(18, 70),
        "residency_status": random.choices(RESIDENCY, weights=[9, 1])[0],
        "sin": _fake_sin(),
        "first_tfsa_year": first_year,
        "past_contributions": random.randint(0, 40000),
        "withdrawals_last_year": random.choice([0, 0, 1000, 2000, 5000]),
        "current_year_contributions": random.randint(0, 7000),
        "checking_balance": round(random.uniform(500, 25000), 2),
    }


def make_transactions(profile: dict) -> list:
    """A small synthetic history consistent-ish with the profile."""
    txns = []
    for year in range(profile["first_tfsa_year"], 2026):
        if random.random() < 0.7:
            txns.append({
                "type": "contribution",
                "year": year,
                "amount": random.choice([1000, 2000, 3000, 5000, 6500, 7000]),
            })
    if profile["withdrawals_last_year"]:
        txns.append({"type": "withdrawal", "year": 2025,
                     "amount": profile["withdrawals_last_year"]})
    return txns


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bucket", default=config.DATA_S3_BUCKET,
                   help="Target S3 bucket (default: config.DATA_S3_BUCKET)")
    p.add_argument("--count", type=int, default=10, help="Number of synthetic users")
    p.add_argument("--profile-prefix", default=config.PROFILE_S3_PREFIX)
    p.add_argument("--txn-prefix", default=config.TRANSACTIONS_S3_PREFIX)
    p.add_argument("--limits-key", default=config.LIMITS_S3_KEY)
    p.add_argument("--region", default=config.DATA_S3_REGION)
    p.add_argument("--id-prefix", default="user_", help="user_id prefix (user_1, user_2, ...)")
    p.add_argument("--dry-run", action="store_true", help="Print what would upload, don't upload")
    args = p.parse_args()

    if not args.bucket and not args.dry_run:
        p.error("--bucket is required (or set DATA_S3_BUCKET), unless --dry-run")

    # Always include the canonical demo user user_123 so existing tests keep working.
    user_ids = ["user_123"] + [f"{args.id_prefix}{i}" for i in range(1, args.count + 1)]

    objects: list[tuple[str, object]] = [(args.limits_key, TFSA_LIMITS)]
    for uid in user_ids:
        profile = make_profile(uid)
        if uid == "user_123":  # keep the well-known demo record stable
            profile.update({"name": "Melanie", "age": 25, "sin": "123-456-789",
                            "first_tfsa_year": 2023})
        objects.append((f"{args.profile_prefix}/{uid}.json", profile))
        objects.append((f"{args.txn_prefix}/{uid}.json", make_transactions(profile)))

    if args.dry_run:
        for key, body in objects:
            print(f"[dry-run] s3://{args.bucket or '<bucket>'}/{key}")
            print(json.dumps(body, indent=2)[:400])
        print(f"\n[dry-run] would upload {len(objects)} objects for {len(user_ids)} users")
        return

    import boto3
    s3 = boto3.client("s3", region_name=args.region)
    for key, body in objects:
        s3.put_object(Bucket=args.bucket, Key=key,
                      Body=json.dumps(body, indent=2).encode("utf-8"),
                      ContentType="application/json")
    print(f"Uploaded {len(objects)} objects to s3://{args.bucket} "
          f"({len(user_ids)} users incl. user_123). Limits at {args.limits_key}.")


if __name__ == "__main__":
    main()
