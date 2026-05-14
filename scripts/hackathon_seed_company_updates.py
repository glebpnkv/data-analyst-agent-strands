"""Seed the hackathon `raw` bucket with a synthetic 'company updates' dataset.

The dataset is deliberately *suboptimal* in a way that mirrors real
internal corporate data feeds — it needs work before it's analysable:

  - The actual numeric payload of each update lives inside a
    pipe-separated key-value blob in the `update_data` column, not as
    its own columns. Schema looks tidy on the surface (always 6
    columns) but the interesting numbers are encoded.
  - `col_count` is fully derivable from `update_type` (4 for
    dividends, 6 for results). It's redundant — that's the point. A
    half-decent EDA pass should surface the redundancy.
  - Numeric ranges are independent draws, so don't expect realistic
    cross-field correlations.

That shape makes it a great demo target for the build-pipeline skill:
download the CSV from S3, drop it into Chainlit's paper-clip during
the demo, then ask for a bronze pipeline that parses `update_data`
into proper columns. Watch the medallion flow happen end-to-end.

The CSV lands at:
    s3://hackathon-da-raw-<acct>-us-east-1/company-updates/data.csv

Pull it down with:
    aws s3 cp s3://hackathon-da-raw-<acct>-us-east-1/company-updates/data.csv .

Re-running overwrites the same key — idempotent.

Usage:
    AWS_PROFILE=hackathon uv run python scripts/hackathon_seed_company_updates.py
"""

from __future__ import annotations

import sys
from datetime import date

import boto3
import numpy as np
import pandas as pd

REGION = "us-east-1"
DEFAULT_ROWS = 5000
SEED = 42

# Recognisable mix of US ($) and EU (€) listings. Currency is fixed
# per ticker — same in real life. ~50/50 split so the agent has
# enough of each to contrast.
_TICKERS: dict[str, str] = {
    # US — NASDAQ / NYSE
    "AAPL": "USD", "MSFT": "USD", "GOOGL": "USD", "AMZN": "USD",
    "META": "USD", "TSLA": "USD", "NVDA": "USD", "JPM":  "USD",
    "BAC":  "USD", "KO":   "USD", "PEP":  "USD", "WMT":  "USD",
    "DIS":  "USD", "XOM":  "USD",
    # EU — XETRA / EPA / AEX / BME (all EUR-denominated)
    "SAP":  "EUR", "ASML": "EUR", "SIE":  "EUR", "MBG":  "EUR",
    "VOW3": "EUR", "BAS":  "EUR", "ADS":  "EUR", "BMW":  "EUR",
    "BNP":  "EUR", "AIR":  "EUR", "TTE":  "EUR", "MC":   "EUR",
    "OR":   "EUR", "SAN":  "EUR", "ALV":  "EUR", "ITX":  "EUR",
}

# Per-update-type field schemas. Order matters — it's the order the
# fields appear in the encoded `update_data` blob.
_DIVIDEND_FIELDS: list[tuple[str, float, float]] = [
    # (name, low, high)
    ("per_share",       0.0,    2.0),
    ("total",           0.0,    1e9),
    ("per_share_delta", -2.0,   2.0),
    ("total_delta",     -1e9,   1e9),
]
_RESULT_FIELDS: list[tuple[str, float, float]] = [
    ("revenue",         0.0,    1e11),
    ("profit",          0.0,    2e10),
    ("tax",             0.0,    5e9),
    ("revenue_delta",   -5e9,   5e9),
    ("profit_delta",    -5e9,   5e9),
    ("tax_delta",       -1e9,   1e9),
]
_FIELDS_BY_TYPE = {"dividends": _DIVIDEND_FIELDS, "results": _RESULT_FIELDS}


def _bucket_name(account: str) -> str:
    return f"hackathon-da-raw-{account}-{REGION}"


def _encode_update_data(pairs: list[tuple[str, float]]) -> str:
    """`[(name, val), ...]` → `'name | val | name | val | ... |'`.

    Trailing pipe is part of the spec — mirrors real feeds where the
    delimiter doubles as a record terminator. Floats render to 2dp to
    keep the encoded string legible without inflating the CSV.
    """
    body = " | ".join(f"{name} | {val:.2f}" for name, val in pairs)
    return body + " |"


def _build_df(n_rows: int = DEFAULT_ROWS, seed: int = SEED) -> pd.DataFrame:
    """Build the synthetic company-updates DataFrame.

    Date is uniformly random in [2026-01-01, today]. update_type is
    50/50 dividends/results. Each row picks a ticker uniformly from
    the hardcoded universe; currency is then a lookup, not a draw.
    """
    rng = np.random.default_rng(seed)
    tickers = list(_TICKERS.keys())

    # Date span — start fixed, end is today (the day the script runs).
    today = pd.Timestamp(date.today())
    start = pd.Timestamp("2026-01-01")
    span_days = max((today - start).days, 1)
    days_offset = rng.integers(0, span_days + 1, size=n_rows)
    dates = start + pd.to_timedelta(days_offset, unit="D")

    company_ids = rng.choice(tickers, size=n_rows)
    currencies = np.array([_TICKERS[t] for t in company_ids])
    update_types = rng.choice(["dividends", "results"], size=n_rows)

    update_data: list[str] = []
    col_counts: list[int] = []
    for kind in update_types:
        fields = _FIELDS_BY_TYPE[kind]
        pairs = [(name, float(rng.uniform(lo, hi))) for name, lo, hi in fields]
        update_data.append(_encode_update_data(pairs))
        col_counts.append(len(pairs))

    return pd.DataFrame(
        {
            # Strip the time component so CSV reads as plain dates
            # (otherwise pandas writes "2026-03-15 00:00:00").
            "date": dates.strftime("%Y-%m-%d"),
            "company_id": company_ids,
            "update_type": update_types,
            "currency": currencies,
            "col_count": col_counts,
            "update_data": update_data,
        }
    )


def _put(s3, bucket: str, key: str, body: bytes, content_type: str) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    print(f"  + s3://{bucket}/{key}  ({len(body):,} bytes, {content_type})")


def main() -> int:
    session = boto3.Session(region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    bucket = _bucket_name(account)
    s3 = session.client("s3")

    print(f"== Seeding raw bucket {bucket} ==\n")

    df = _build_df()
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    _put(s3, bucket, "company-updates/data.csv", csv_bytes, "text/csv")

    print(f"\n  Verify with:")
    print(f"    AWS_PROFILE=hackathon aws s3 ls s3://{bucket}/company-updates/")
    print(f"  Pull locally with:")
    print(f"    AWS_PROFILE=hackathon aws s3 cp s3://{bucket}/company-updates/data.csv .")
    return 0


if __name__ == "__main__":
    sys.exit(main())
