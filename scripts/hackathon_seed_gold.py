"""Seed the hackathon `gold` bucket with two demo datasets.

Both are CSV — keeps the demo on a single file format end-to-end. The
sandbox can read CSV with no extra packages, the deployed Lambdas use
the stdlib `csv` module, and Chainlit's drop-zone is also CSV/Excel.

  - `synthetic-regression/data.csv`: 1000 rows, 6 numeric features + a
    target column generated from a known linear+sinusoidal formula.
    Good for EDA showing summary stats, distributions, correlations,
    and the agent surfacing the deliberately-zero-coefficient feature.
  - `monthly-sales/data.csv`: 24 months × 4 regions × 1 product family.
    Time-series + categorical mix, good for "show me sales by region
    over time" plotting.

Re-running overwrites the same keys — idempotent.

Usage:
    AWS_PROFILE=hackathon uv run python scripts/hackathon_seed_gold.py
"""

from __future__ import annotations

import sys

import boto3
import numpy as np
import pandas as pd

REGION = "us-east-1"


def _bucket_name(account: str) -> str:
    return f"hackathon-da-gold-{account}-{REGION}"


def _build_regression_df(n_rows: int = 1000, seed: int = 42) -> pd.DataFrame:
    """Synthetic regression data from upload_sample_data.py's recipe.

    `y = X[:, :5] @ beta[:5] + sin(X[:, 5]) * beta[5] + eps`. beta[2] is
    deliberately 0, so the agent can demonstrate detecting the dead
    feature via correlation analysis or feature importance.
    """
    rng = np.random.default_rng(seed)
    betas = np.array([1.2, -0.8, 0.0, 1.5, -1.1, 0.9])
    x = rng.standard_normal(size=(n_rows, 6))
    eps = rng.standard_normal(size=n_rows)
    y = x[:, :5] @ betas[:5] + np.sin(x[:, 5]) * betas[5] + eps
    return pd.DataFrame(
        {
            **{f"x{i + 1}": x[:, i] for i in range(6)},
            "output": y,
        }
    )


def _build_sales_df(seed: int = 7) -> pd.DataFrame:
    """24 months × 4 regions of synthetic monthly product sales.

    Includes a clear regional baseline gap and a mild seasonal pattern
    (sin over the month index) plus per-row noise — enough structure for
    the agent to surface in a few minutes of EDA.
    """
    rng = np.random.default_rng(seed)
    months = pd.date_range(start="2024-01-01", periods=24, freq="MS").to_pydatetime()
    regions = ["EMEA", "NA", "APAC", "LATAM"]
    region_baseline = {"EMEA": 12000, "NA": 18000, "APAC": 9000, "LATAM": 5500}
    rows: list[dict] = []
    for m_idx, month in enumerate(months):
        seasonal = 1.0 + 0.18 * np.sin(2 * np.pi * m_idx / 12)
        for region in regions:
            noise = rng.normal(loc=1.0, scale=0.08)
            revenue = region_baseline[region] * seasonal * noise
            units = int(revenue / rng.uniform(45, 65))
            rows.append(
                {
                    "month": month.date().isoformat(),
                    "region": region,
                    "product_family": "Widgets",
                    "units": units,
                    "revenue": round(revenue, 2),
                }
            )
    return pd.DataFrame(rows)


def _put(s3, bucket: str, key: str, body: bytes, content_type: str) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    print(f"  + s3://{bucket}/{key}  ({len(body):,} bytes, {content_type})")


def main() -> int:
    session = boto3.Session(region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    bucket = _bucket_name(account)
    s3 = session.client("s3")

    print(f"== Seeding gold bucket {bucket} ==\n")

    reg_df = _build_regression_df()
    reg_csv = reg_df.to_csv(index=False).encode("utf-8")
    _put(s3, bucket, "synthetic-regression/data.csv", reg_csv, "text/csv")

    sales_df = _build_sales_df()
    sales_csv = sales_df.to_csv(index=False).encode("utf-8")
    _put(s3, bucket, "monthly-sales/data.csv", sales_csv, "text/csv")

    print(f"\n  Datasets ready. Verify with:")
    print(f"    AWS_PROFILE=hackathon aws s3 ls s3://{bucket}/ --recursive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
