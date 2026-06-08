"""Generate a small synthetic sales schema and load it into Athena.

Three tables under a single Glue database (`eval_sales`):

  customers  (customer_id PK, name, region, segment, signup_date)
  products   (product_id PK, name, category, unit_price)
  orders     (order_id PK, customer_id FK, product_id FK,
              order_date, quantity, line_total)

The schema is deliberately small and join-heavy so goldens can exercise:
  - simple joins   (orders → customers by region)
  - 3-way joins    (orders × customers × products)
  - aggregates with grouping (sum revenue by category, top customers)
  - time windows   (orders this quarter vs last)

Synthetic data so no PII concerns. Deterministic when --seed is passed.

Usage:
    uv run --group dev python scripts/upload_sales_data.py \
        --bucket langchain-strands \
        --seed 42
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import awswrangler as wr
import boto3
import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

DEFAULT_DATABASE = "eval_sales"
DEFAULT_PREFIX = "eval/sales/"

# Sizes — small enough that the agent can scan the whole table cheaply
# and large enough that aggregates produce non-trivial group sizes.
N_CUSTOMERS = 200
N_PRODUCTS = 30
N_ORDERS = 5_000

REGIONS = ["EMEA", "AMER", "APAC"]
SEGMENTS = ["enterprise", "midmarket", "smb"]
CATEGORIES = ["hardware", "software", "subscription", "services"]


@dataclass
class SalesData:
    customers: pd.DataFrame
    products: pd.DataFrame
    orders: pd.DataFrame


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="S3 bucket name (no s3://)")
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"S3 prefix under the bucket. Default: {DEFAULT_PREFIX}",
    )
    parser.add_argument(
        "--database",
        default=DEFAULT_DATABASE,
        help=f"Glue/Athena database. Default: {DEFAULT_DATABASE}",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed. Default: 42 (so reruns produce the same rows).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    data = _build_sales(seed=args.seed)
    log.info(
        "Generated synthetic sales: %d customers, %d products, %d orders",
        len(data.customers),
        len(data.products),
        len(data.orders),
    )

    session = boto3.Session()
    wr.catalog.create_database(name=args.database, exist_ok=True, boto3_session=session)

    for table_name, df in [
        ("customers", data.customers),
        ("products", data.products),
        ("orders", data.orders),
    ]:
        s3_path = f"s3://{args.bucket}/{args.prefix.strip('/')}/{table_name}/"
        result = wr.s3.to_parquet(
            df=df,
            path=s3_path,
            dataset=True,
            mode="overwrite",
            database=args.database,
            table=table_name,
            sanitize_columns=True,
            boto3_session=session,
        )
        log.info("✅ %s.%s — %d rows, %d files at %s",
                 args.database, table_name, len(df), len(result.get("paths", [])), s3_path)
    return 0


def _build_sales(*, seed: int) -> SalesData:
    rng = np.random.default_rng(seed)

    # Customers — names are fake but stable.
    customers = pd.DataFrame({
        "customer_id": np.arange(1, N_CUSTOMERS + 1, dtype=np.int64),
        "name": [f"Customer-{i:04d}" for i in range(1, N_CUSTOMERS + 1)],
        "region": rng.choice(REGIONS, size=N_CUSTOMERS, p=[0.5, 0.3, 0.2]),
        "segment": rng.choice(SEGMENTS, size=N_CUSTOMERS, p=[0.2, 0.3, 0.5]),
        "signup_date": _random_dates(rng, start="2022-01-01", end="2024-12-31", n=N_CUSTOMERS),
    })

    # Products — unit prices skewed by category.
    cat_choices = rng.choice(CATEGORIES, size=N_PRODUCTS)
    price_lookup = {"hardware": (500, 5000), "software": (50, 800), "subscription": (10, 200), "services": (1000, 10000)}
    unit_prices = []
    for cat in cat_choices:
        low, high = price_lookup[cat]
        unit_prices.append(round(float(rng.uniform(low, high)), 2))
    products = pd.DataFrame({
        "product_id": np.arange(1, N_PRODUCTS + 1, dtype=np.int64),
        "name": [f"Product-{i:03d}" for i in range(1, N_PRODUCTS + 1)],
        "category": cat_choices,
        "unit_price": unit_prices,
    })

    # Orders — random customer × product joins, last 18 months.
    customer_ids = rng.integers(1, N_CUSTOMERS + 1, size=N_ORDERS, dtype=np.int64)
    product_ids = rng.integers(1, N_PRODUCTS + 1, size=N_ORDERS, dtype=np.int64)
    quantities = rng.integers(1, 11, size=N_ORDERS, dtype=np.int64)
    order_dates = _random_dates(rng, start="2024-07-01", end="2026-01-31", n=N_ORDERS)
    unit_price_by_id = dict(zip(products["product_id"], products["unit_price"]))
    line_totals = np.array(
        [round(unit_price_by_id[p] * q, 2) for p, q in zip(product_ids, quantities)],
        dtype=np.float64,
    )
    orders = pd.DataFrame({
        "order_id": np.arange(1, N_ORDERS + 1, dtype=np.int64),
        "customer_id": customer_ids,
        "product_id": product_ids,
        "order_date": order_dates,
        "quantity": quantities,
        "line_total": line_totals,
    })

    return SalesData(customers=customers, products=products, orders=orders)


def _random_dates(rng: np.random.Generator, *, start: str, end: str, n: int) -> list[str]:
    """Uniform random dates between start and end (inclusive), formatted YYYY-MM-DD."""
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    span_days = (end_dt - start_dt).days
    offsets = rng.integers(0, span_days + 1, size=n)
    return [(start_dt + timedelta(days=int(d))).strftime("%Y-%m-%d") for d in offsets]


if __name__ == "__main__":
    raise SystemExit(main())
