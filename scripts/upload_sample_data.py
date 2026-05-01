import argparse
import logging

import awswrangler as wr
import boto3
import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_DATABASE = "sample_database"
DEFAULT_TABLE = "sample_table"
DEFAULT_PREFIX = "demo/sample_table/"
MAX_ROWS = 1_000_000

FEATURE_COLUMNS = [f"x{i}" for i in range(1, 7)]
OUTPUT_COLUMN = "output"
ALL_COLUMNS = [*FEATURE_COLUMNS, OUTPUT_COLUMN]

# y = X_1:5 * beta_1:5 + sin(X_6) * beta_6 + eps
# beta_3 is intentionally 0.
BETAS = np.array([1.2, -0.8, 0.0, 1.5, -1.1, 0.9], dtype=np.float64)


def to_s3_prefix(bucket: str, prefix: str) -> str:
    """Build a normalized S3 URI from bucket and prefix."""
    return f"s3://{bucket}/{prefix.strip('/')}/"


def clamp_rows(n: int) -> int:
    """Validate n and cap it to MAX_ROWS."""
    if n <= 0:
        raise ValueError("n must be a positive integer")
    if n > MAX_ROWS:
        logger.warning("Requested n=%s is above %s; capping to %s", n, MAX_ROWS, MAX_ROWS)
        return MAX_ROWS
    return n


def ensure_database(glue_client, database: str) -> bool:
    """Ensure the Glue database exists. Returns True when created."""
    try:
        glue_client.get_database(Name=database)
        return False
    except glue_client.exceptions.EntityNotFoundException:
        glue_client.create_database(DatabaseInput={"Name": database})
        return True


def ensure_table(glue_client, database: str, table: str, s3_path: str) -> tuple[bool, str]:
    """Ensure the Glue table exists with expected schema and return (created, table_path)."""
    try:
        response = glue_client.get_table(DatabaseName=database, Name=table)
        storage_descriptor = response["Table"].get("StorageDescriptor", {})
        existing_columns = [col["Name"] for col in storage_descriptor.get("Columns", [])]
        missing_columns = [col for col in ALL_COLUMNS if col not in existing_columns]
        if missing_columns:
            raise ValueError(
                f"Existing table '{database}.{table}' is missing required columns: {missing_columns}"
            )
        existing_path = storage_descriptor.get("Location")
        table_path = (existing_path or s3_path).rstrip("/") + "/"
        return False, table_path
    except glue_client.exceptions.EntityNotFoundException:
        glue_client.create_table(
            DatabaseName=database,
            TableInput={
                "Name": table,
                "TableType": "EXTERNAL_TABLE",
                "StorageDescriptor": {
                    "Columns": [{"Name": column, "Type": "double"} for column in ALL_COLUMNS],
                    "Location": s3_path,
                    "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                    "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                    "SerdeInfo": {
                        "SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
                        "Parameters": {"serialization.format": "1"},
                    },
                },
                "Parameters": {
                    "classification": "parquet",
                    "EXTERNAL": "TRUE",
                },
            },
        )
        return True, s3_path


def build_sample_df(n_rows: int, seed: int | None = None) -> pd.DataFrame:
    """Generate sample rows with x1..x6 and output from the configured synthetic formula."""
    rng = np.random.default_rng(seed)

    # Generate feature matrix X and Gaussian noise eps from standard normal.
    x = rng.standard_normal(size=(n_rows, 6))
    eps = rng.standard_normal(size=n_rows)

    # y = X_1:5 * beta_1:5 + sin(X_6) * beta_6 + eps
    linear_component = x[:, :5] @ BETAS[:5]
    nonlinear_component = np.sin(x[:, 5]) * BETAS[5]
    output = linear_component + nonlinear_component + eps

    # Emit x1..x6 and output columns in a stable order.
    data = {column: x[:, idx] for idx, column in enumerate(FEATURE_COLUMNS)}
    data[OUTPUT_COLUMN] = output
    return pd.DataFrame(data)


def parse_args():
    """Parse command-line arguments for data generation and upload."""
    parser = argparse.ArgumentParser()
    parser.add_argument("n", type=int, help=f"Number of rows to generate (max {MAX_ROWS:,})")
    parser.add_argument("--bucket", required=True, help="S3 bucket name (no s3://)")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="S3 prefix for table data")
    parser.add_argument("--database", default=DEFAULT_DATABASE, help="Glue/Athena database")
    parser.add_argument("--table", default=DEFAULT_TABLE, help="Glue/Athena table")
    parser.add_argument("--seed", type=int, default=None, help="Optional random seed")
    return parser.parse_args()


def main():
    """Create catalog resources if needed, then append generated rows to the Glue table."""
    args = parse_args()
    n_rows = clamp_rows(args.n)

    # Reuse one boto3 session for Glue catalog + S3 writes.
    session = boto3.Session()
    glue_client = session.client("glue")

    s3_path = to_s3_prefix(bucket=args.bucket, prefix=args.prefix)

    # Ensure catalog resources exist before writing data.
    database_created = ensure_database(glue_client=glue_client, database=args.database)
    table_created, table_path = ensure_table(
        glue_client=glue_client,
        database=args.database,
        table=args.table,
        s3_path=s3_path,
    )

    if not table_created and table_path != s3_path:
        logger.info(
            "Table already exists at %s; ignoring --bucket/--prefix location %s",
            table_path,
            s3_path,
        )

    # Generate the requested sample rows.
    df = build_sample_df(n_rows=n_rows, seed=args.seed)

    # Append rows as a parquet dataset and keep Glue table metadata in sync.
    result = wr.s3.to_parquet(
        df=df,
        path=table_path,
        dataset=True,
        mode="append",
        database=args.database,
        table=args.table,
        sanitize_columns=True,
        boto3_session=session,
    )

    logger.info("✅ Sample data upload complete")
    logger.info("Database: %s (%s)", args.database, "created" if database_created else "already existed")
    logger.info("Table: %s (%s)", args.table, "created" if table_created else "already existed")
    logger.info("S3 path: %s", table_path)
    logger.info("Rows appended: %s", len(df))
    logger.info("Files written: %s", len(result.get("paths", [])))
    logger.info("Betas used: %s", BETAS.tolist())


if __name__ == "__main__":
    main()
