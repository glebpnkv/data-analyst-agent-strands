"""Load NYC TLC yellow-taxi trip records into Athena for eval use.

Source: NYC TLC's public CloudFront mirror (one parquet file per month).
We pull a single month, write it to our data bucket as parquet, and
register `eval_taxi.taxi_trips` in the Glue catalog. The agent then
sees a real-world dataset with time-of-day, distance, and fare columns
so goldens can ask aggregation questions ("what was the average fare on
Sundays") that the iris goldens can't.

Usage:
    uv run --group dev python scripts/upload_taxi_data.py \
        --bucket langchain-strands \
        --month 2024-01

Idempotent on (database, table, month): re-running with the same month
overwrites the parquet under the same S3 prefix and updates the table
in place. Use a different `--month` to load additional months — they
go under separate partitions if `--partition-by-month` is passed.
"""

from __future__ import annotations

import argparse
import io
import logging
from datetime import datetime

import awswrangler as wr
import boto3
import pandas as pd
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

DEFAULT_DATABASE = "eval_taxi"
DEFAULT_TABLE = "taxi_trips"
DEFAULT_PREFIX = "eval/taxi/"
TLC_URL_TEMPLATE = (
    "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_{month}.parquet"
)


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
        "--table",
        default=DEFAULT_TABLE,
        help=f"Glue/Athena table. Default: {DEFAULT_TABLE}",
    )
    parser.add_argument(
        "--month",
        default="2024-01",
        help="Yellow-taxi month to load, YYYY-MM. Default: 2024-01 (~3M rows, ~50 MB).",
    )
    parser.add_argument(
        "--sample-rows",
        type=int,
        default=None,
        help="If set, downsample to this many rows before uploading (handy for cheaper Athena scans).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _validate_month(args.month)

    df = _download_month(args.month)
    log.info("Downloaded %d rows for %s", len(df), args.month)

    if args.sample_rows is not None and args.sample_rows < len(df):
        df = df.sample(n=args.sample_rows, random_state=42).reset_index(drop=True)
        log.info("Sampled down to %d rows", len(df))

    # Normalise column names — TLC's parquet has CamelCase / spaces which
    # makes SQL prompts noisier. snake_case mapping keeps prompts clean.
    df = _normalise_columns(df)

    session = boto3.Session()
    wr.catalog.create_database(name=args.database, exist_ok=True, boto3_session=session)

    s3_path = f"s3://{args.bucket}/{args.prefix.strip('/')}/"
    result = wr.s3.to_parquet(
        df=df,
        path=s3_path,
        dataset=True,
        mode="overwrite",
        database=args.database,
        table=args.table,
        sanitize_columns=True,
        boto3_session=session,
    )

    log.info("✅ Uploaded NYC TLC yellow-taxi data for Athena")
    log.info("Database: %s", args.database)
    log.info("Table: %s", args.table)
    log.info("S3 path: %s", s3_path)
    log.info("Rows: %d, files: %d", len(df), len(result.get("paths", [])))
    return 0


def _validate_month(month: str) -> None:
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError:
        raise SystemExit(f"--month must be YYYY-MM, got: {month!r}")


def _download_month(month: str) -> pd.DataFrame:
    url = TLC_URL_TEMPLATE.format(month=month)
    log.info("GET %s", url)
    # The parquet files are typically 50 MB; in-memory is fine and faster
    # than a tmpfile round-trip on a developer laptop.
    with urllib.request.urlopen(url) as resp:
        body = resp.read()
    return pd.read_parquet(io.BytesIO(body))


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "tpep_pickup_datetime": "pickup_datetime",
        "tpep_dropoff_datetime": "dropoff_datetime",
        "VendorID": "vendor_id",
        "PULocationID": "pickup_location_id",
        "DOLocationID": "dropoff_location_id",
        "RatecodeID": "rate_code_id",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    df.columns = [c.lower() for c in df.columns]
    return df


if __name__ == "__main__":
    raise SystemExit(main())
