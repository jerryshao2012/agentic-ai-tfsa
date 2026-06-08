#!/usr/bin/env python3
"""
Generate enriched synthetic TFSA data and upload it to S3 in the layout the agent expects.

Produces, for N users:
  s3://{bucket}/{profile_prefix}/{user_id}.json        - profile dict (rich)
  s3://{bucket}/{txn_prefix}/{user_id}.json            - list of contribution/withdrawal txns
  s3://{bucket}/{limits_key}                            - {"2009": 5000, ...}

Key properties of the dataset:
  * user_ids are RANDOM (e.g. user_481073), not sequential, so successive runs don't
    silently overwrite each other. user_123 is always included as the stable demo record.
  * Each user is built from a persona (maxed-out, over-contributor, retiree, new immigrant,
    young saver, non-resident, frequent trader, balanced) so the demo set is varied.
  * Transactions are generated FIRST (with ISO dates, channel, and a running balance), then
    the profile aggregates (past_contributions / current_year_contributions /
    withdrawals_last_year / tfsa_balance) are DERIVED from them, so the room math the agent
    performs reconciles with the visible transaction history.

The agent reads these via data_sources.py when DATA_S3_BUCKET is set. This script is the
reproducible source of the demo dataset — re-run it to reset/extend the data.

Usage:
  python scripts/upload_synthetic_data.py --bucket my-bucket --count 25
  python scripts/upload_synthetic_data.py --bucket my-bucket --purge        # delete existing data first
  python scripts/upload_synthetic_data.py --bucket my-bucket --dry-run      # print, don't upload
  python scripts/upload_synthetic_data.py --bucket my-bucket --seed 7       # reproducible run

Defaults for prefixes/keys/region come from config.py (env-overridable). Requires boto3 and
credentials with s3:PutObject (and s3:DeleteObject + s3:ListBucket for --purge) on the bucket.
"""
import argparse
import datetime
import json
import os
import random
import sys

# Allow running from anywhere: import config from the package dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

CURRENT_YEAR = datetime.datetime.now().year

# Canonical TFSA annual limits (the reference dataset).
TFSA_LIMITS = {
    "2009": 5000, "2010": 5000, "2011": 5000, "2012": 5000,
    "2013": 5500, "2014": 5500, "2015": 10000, "2016": 5500,
    "2017": 5500, "2018": 5500, "2019": 6000, "2020": 6000,
    "2021": 6000, "2022": 6000, "2023": 6500, "2024": 7000,
    "2025": 7000, "2026": 7000,
}
_LIMITS_INT = {int(y): v for y, v in TFSA_LIMITS.items()}

FIRST_NAMES = ["Melanie", "Arjun", "Sofia", "Liam", "Noah", "Priya", "Wei", "Fatima",
               "Diego", "Chloe", "Omar", "Hana", "Lucas", "Ava", "Mateo", "Yuki",
               "David", "Ravi", "Elena", "Tariq", "Mia", "Sven", "Aisha", "Carlos",
               "Ingrid", "Hannah", "Leila", "Pierre", "Sara", "Tomas", "Nadia", "Kenji",
               "Grace", "Hassan", "Olivia", "Dimitri"]
LAST_NAMES = ["Tremblay", "Smith", "Patel", "Nguyen", "Garcia", "Khan", "Wang", "Okafor",
              "Brown", "Singh", "Rossi", "Kim", "Dubois", "Silva", "Cohen", "Ali",
              "Andersson", "Lopez", "Mueller", "Hassan", "Chen", "Martin", "Reyes", "Ivanov"]

# (province_code, city) pairs for plausible Canadian addresses.
PROVINCES = [
    ("ON", "Toronto"), ("ON", "Ottawa"), ("BC", "Vancouver"), ("BC", "Victoria"),
    ("AB", "Calgary"), ("AB", "Edmonton"), ("QC", "Montreal"), ("QC", "Quebec City"),
    ("MB", "Winnipeg"), ("SK", "Saskatoon"), ("NS", "Halifax"), ("NB", "Fredericton"),
]
EMPLOYMENT = ["Employed full-time", "Employed part-time", "Self-employed", "Student",
              "Retired", "Unemployed"]
MARITAL = ["Single", "Married", "Common-law", "Divorced", "Widowed"]
RISK = ["Conservative", "Balanced", "Growth", "Aggressive"]
GOALS = ["Retirement", "Home down payment", "Emergency fund", "Education",
         "Wealth building", "Travel"]
INSTITUTIONS = ["BMO", "RBC", "TD", "Scotiabank", "CIBC"]
ADVISORS = ["Jordan Lee", "Sam Carter", "Robin Patel", "Alex Morgan", "Casey Nguyen",
            "Taylor Brooks"]
CHANNELS = ["online_banking", "mobile_app", "branch", "pre_authorized", "advisor"]

# Persona mix controls how contributions/withdrawals are generated. Weights bias the
# random draw so "balanced" is the common case and the edge cases show up occasionally.
PERSONAS = ["balanced", "max_contributor", "over_contributor", "retiree",
            "new_immigrant", "young_saver", "non_resident", "frequent_trader"]
PERSONA_WEIGHTS = [40, 12, 6, 10, 12, 10, 4, 6]


def _fake_sin() -> str:
    return f"{random.randint(100, 999)}-{random.randint(100, 999)}-{random.randint(100, 999)}"


def _email(name: str, uid: str) -> str:
    handle = name.lower().replace(" ", ".")
    return f"{handle}.{uid.split('_')[-1]}@example.com"


def _phone() -> str:
    return f"+1-{random.randint(200, 999)}-{random.randint(200, 999)}-{random.randint(1000, 9999)}"


def _txn_date(year: int) -> str:
    """A plausible ISO date within `year` (capped at today for the current year)."""
    if year == CURRENT_YEAR:
        end = datetime.date.today()
    else:
        end = datetime.date(year, 12, 28)
    start = datetime.date(year, 1, 1)
    delta = (end - start).days
    return (start + datetime.timedelta(days=random.randint(0, max(delta, 0)))).isoformat()


def _persona_params(persona: str):
    """Return (age, first_tfsa_year, residency, employment) seeded by persona."""
    if persona == "retiree":
        age = random.randint(60, 72)
        first = random.choice([2009, 2010, 2011])
        return age, first, "Canadian Resident", "Retired"
    if persona == "new_immigrant":
        age = random.randint(28, 45)
        first = random.choice([2023, 2024, 2025])
        return age, first, "Canadian Resident", random.choice(EMPLOYMENT[:3])
    if persona == "young_saver":
        age = random.randint(18, 26)
        first = random.choice([2022, 2023, 2024, 2025])
        return age, first, "Canadian Resident", random.choice(["Student", "Employed part-time", "Employed full-time"])
    if persona == "non_resident":
        age = random.randint(30, 55)
        first = random.choice([2015, 2016, 2018, 2020])
        return age, first, "Non-Resident", random.choice(EMPLOYMENT[:3])
    # balanced / max / over / frequent_trader
    age = random.randint(25, 58)
    first = random.choice([2009, 2012, 2015, 2018, 2020, 2021, 2023])
    return age, first, "Canadian Resident", random.choice(EMPLOYMENT[:4])


def _eligible_years(age: int, first_tfsa_year: int) -> list[int]:
    """Years (inclusive of CURRENT_YEAR) in which the user could contribute."""
    birth_year = CURRENT_YEAR - age
    start = max(first_tfsa_year, birth_year + 18, 2009)
    return [y for y in range(start, CURRENT_YEAR + 1)]


def _contribution_for(persona: str, year: int, limit: int) -> int:
    """Pick a contribution amount for a given year, shaped by persona."""
    if persona == "max_contributor":
        return limit
    if persona == "over_contributor":
        # Mostly maxes out; the most recent year over-contributes (penalty demo).
        return limit if year < CURRENT_YEAR else limit + random.choice([500, 1000, 2000])
    if persona == "young_saver":
        return random.choice([0, 0, 500, 1000, 1500, 2000])
    if persona == "new_immigrant":
        return random.choice([0, 1000, 2000, 3000, limit])
    if persona == "non_resident":
        # Non-residents accrue no new room; keep contributions sparse/small.
        return random.choice([0, 0, 0, 1000])
    if persona == "frequent_trader":
        return random.choice([1000, 2000, 3000, 4000, limit])
    # balanced
    return random.choice([0, 1000, 2000, 3000, 5000, limit]) if random.random() < 0.75 else 0


def make_transactions(persona: str, age: int, first_tfsa_year: int) -> list:
    """Build a dated, balance-tracked transaction history for the user.

    Returns a list of transactions sorted by date. Each contribution/withdrawal carries an
    ISO date, channel, human-readable description, and the running TFSA balance afterwards.
    """
    events: list[dict] = []
    for year in _eligible_years(age, first_tfsa_year):
        limit = _LIMITS_INT.get(year, 7000)
        amount = _contribution_for(persona, year, limit)
        if amount:
            events.append({"type": "contribution", "year": year,
                           "date": _txn_date(year), "amount": amount})
        # Withdrawals: frequent_trader and retiree withdraw more often; others rarely.
        # over_contributor never withdraws so its over-contribution cleanly shows as
        # negative available room (penalty demo) without a withdrawal adding room back.
        wd_prob = {"frequent_trader": 0.45, "retiree": 0.45, "over_contributor": 0.0}.get(persona, 0.12)
        if year < CURRENT_YEAR and random.random() < wd_prob:
            events.append({"type": "withdrawal", "year": year,
                           "date": _txn_date(year),
                           "amount": random.choice([500, 1000, 1500, 2000, 3000])})

    events.sort(key=lambda e: e["date"])
    balance = 0.0
    txns = []
    for e in events:
        if e["type"] == "contribution":
            balance += e["amount"]
            desc = f"TFSA contribution ({e['year']})"
        else:
            balance = max(0.0, balance - e["amount"])
            desc = f"TFSA withdrawal ({e['year']})"
        txns.append({
            "type": e["type"],
            "year": e["year"],
            "date": e["date"],
            "amount": e["amount"],
            "channel": random.choice(CHANNELS),
            "description": desc,
            "balance_after": round(balance, 2),
        })
    return txns


def _derive_aggregates(txns: list) -> dict:
    """Compute profile aggregates from the transaction list so the two reconcile."""
    past = sum(t["amount"] for t in txns
               if t["type"] == "contribution" and t["year"] < CURRENT_YEAR)
    current = sum(t["amount"] for t in txns
                  if t["type"] == "contribution" and t["year"] == CURRENT_YEAR)
    wd_last = sum(t["amount"] for t in txns
                  if t["type"] == "withdrawal" and t["year"] == CURRENT_YEAR - 1)
    contribs = sum(t["amount"] for t in txns if t["type"] == "contribution")
    withdrawals = sum(t["amount"] for t in txns if t["type"] == "withdrawal")
    return {
        "past_contributions": past,
        "current_year_contributions": current,
        "withdrawals_last_year": wd_last,
        "tfsa_balance": round(max(0.0, contribs - withdrawals), 2),
    }


def make_profile(user_id: str, txns: list, age: int, first_tfsa_year: int,
                 residency: str, employment: str, persona: str) -> dict:
    name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    province, city = random.choice(PROVINCES)
    agg = _derive_aggregates(txns)
    # Account opened in the user's first eligible year, plausible month/day.
    opened = datetime.date(first_tfsa_year, random.randint(1, 12), random.randint(1, 28))
    return {
        "user_id": user_id,
        "name": name,
        "age": age,
        "residency_status": residency,
        "sin": _fake_sin(),
        "email": _email(name, user_id),
        "phone": _phone(),
        "province": province,
        "city": city,
        "marital_status": random.choice(MARITAL),
        "employment_status": employment,
        "annual_income": random.choice([35000, 48000, 62000, 75000, 90000, 120000, 150000]),
        "first_tfsa_year": first_tfsa_year,
        "account_open_date": opened.isoformat(),
        "institution": random.choice(INSTITUTIONS),
        "advisor_name": random.choice(ADVISORS),
        "risk_tolerance": random.choice(RISK),
        "investment_goal": random.choice(GOALS),
        "checking_balance": round(random.uniform(500, 25000), 2),
        "persona": persona,
        **agg,
    }


def _make_user_id(existing: set) -> str:
    while True:
        uid = f"user_{random.randint(100000, 999999)}"
        if uid not in existing:
            existing.add(uid)
            return uid


def purge_existing(s3, bucket: str, profile_prefix: str, txn_prefix: str) -> int:
    """Delete all objects under the profile and transaction prefixes. Returns count deleted."""
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in (profile_prefix.rstrip("/") + "/", txn_prefix.rstrip("/") + "/"):
        batch = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                batch.append({"Key": obj["Key"]})
                if len(batch) == 1000:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})
                    deleted += len(batch)
                    batch = []
        if batch:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})
            deleted += len(batch)
    return deleted


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bucket", default=config.DATA_S3_BUCKET,
                   help="Target S3 bucket (default: config.DATA_S3_BUCKET)")
    p.add_argument("--count", type=int, default=10, help="Number of synthetic users (excl. user_123)")
    p.add_argument("--profile-prefix", default=config.PROFILE_S3_PREFIX)
    p.add_argument("--txn-prefix", default=config.TRANSACTIONS_S3_PREFIX)
    p.add_argument("--limits-key", default=config.LIMITS_S3_KEY)
    p.add_argument("--region", default=config.DATA_S3_REGION)
    p.add_argument("--purge", action="store_true",
                   help="Delete existing objects under the profile/txn prefixes before uploading")
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible output")
    p.add_argument("--dry-run", action="store_true", help="Print what would upload, don't upload")
    args = p.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    if not args.bucket and not args.dry_run:
        p.error("--bucket is required (or set DATA_S3_BUCKET), unless --dry-run")

    # Random, unique user_ids; user_123 is always included as the stable demo record.
    seen = {"user_123"}
    user_ids = ["user_123"] + [_make_user_id(seen) for _ in range(args.count)]

    objects: list[tuple[str, object]] = [(args.limits_key, TFSA_LIMITS)]
    for uid in user_ids:
        if uid == "user_123":
            # Stable, well-known demo persona so existing tests/demos stay deterministic.
            persona = "balanced"
            age, first_year, residency, employment = 25, 2023, "Canadian Resident", "Employed full-time"
        else:
            persona = random.choices(PERSONAS, weights=PERSONA_WEIGHTS)[0]
            age, first_year, residency, employment = _persona_params(persona)

        txns = make_transactions(persona, age, first_year)
        profile = make_profile(uid, txns, age, first_year, residency, employment, persona)
        if uid == "user_123":
            profile.update({"name": "Melanie", "sin": "123-456-789", "email": "melanie@example.com"})

        objects.append((f"{args.profile_prefix}/{uid}.json", profile))
        objects.append((f"{args.txn_prefix}/{uid}.json", txns))

    if args.dry_run:
        for key, body in objects:
            print(f"[dry-run] s3://{args.bucket or '<bucket>'}/{key}")
            print(json.dumps(body, indent=2)[:600])
        print(f"\n[dry-run] would upload {len(objects)} objects for {len(user_ids)} users "
              f"(purge={args.purge})")
        return

    import boto3
    s3 = boto3.client("s3", region_name=args.region)

    if args.purge:
        n = purge_existing(s3, args.bucket, args.profile_prefix, args.txn_prefix)
        print(f"Purged {n} existing object(s) under {args.profile_prefix}/ and {args.txn_prefix}/.")

    for key, body in objects:
        s3.put_object(Bucket=args.bucket, Key=key,
                      Body=json.dumps(body, indent=2).encode("utf-8"),
                      ContentType="application/json")
    print(f"Uploaded {len(objects)} objects to s3://{args.bucket} "
          f"({len(user_ids)} users incl. user_123). Limits at {args.limits_key}.")
    print(f"Sample user_ids: {', '.join(user_ids[:6])}{' ...' if len(user_ids) > 6 else ''}")


if __name__ == "__main__":
    main()
