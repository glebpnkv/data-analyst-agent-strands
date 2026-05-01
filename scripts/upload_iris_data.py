import argparse
import logging

import awswrangler as wr
import boto3
from sklearn.datasets import load_iris

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def build_iris_df():
    iris = load_iris(as_frame=True)  # returns .frame when as_frame=True
    df = iris.frame.copy()

    # Rename columns to SQL-friendly names (avoids spaces/parentheses in prompts + SQL)
    rename_map = {
        "sepal length (cm)": "sepal_length_cm",
        "sepal width (cm)": "sepal_width_cm",
        "petal length (cm)": "petal_length_cm",
        "petal width (cm)": "petal_width_cm",
        "target": "target_id",
    }
    df = df.rename(columns=rename_map)

    # Add human-readable label
    target_names = list(iris.target_names)
    df["species"] = df["target_id"].map(lambda i: target_names[int(i)])

    # Stable primary key for easy demos
    df.insert(0, "row_id", range(1, len(df) + 1))
    return df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True, help="S3 bucket name (no s3://)")
    parser.add_argument("--prefix", default="demo/iris/", help="S3 prefix for dataset")
    parser.add_argument("--database", default="iris_demo", help="Glue/Athena database")
    parser.add_argument("--table", default="iris", help="Glue/Athena table")
    parser.add_argument(
        "--partition-by-species",
        action="store_true",
        help="Partition dataset by species (not needed for tiny iris, but useful for demoing partitions)",
    )
    args = parser.parse_args()

    session = boto3.Session()

    df = build_iris_df()

    # Ensure Glue database exists
    wr.catalog.create_database(
        name=args.database,
        exist_ok=True,
        boto3_session=session,
    )

    s3_path = f"s3://{args.bucket}/{args.prefix.strip('/')}/"

    write_kwargs = dict(
        df=df.copy(),
        path=s3_path,
        dataset=True,
        mode="overwrite",
        database=args.database,
        table=args.table,
        sanitize_columns=True,
        boto3_session=session,
    )

    if args.partition_by_species:
        write_kwargs["partition_cols"] = ["species"]

    result = wr.s3.to_parquet(**write_kwargs)

    logger.info("✅ Uploaded iris dataset for Athena")
    logger.info(f"Database: {args.database}")
    logger.info(f"Table: {args.table}")
    logger.info(f"S3 path: {s3_path}")
    logger.info(f"Files: {len(result.get('paths', []))}")
    if result.get("partitions_values"):
        logger.info(f"Partitions: {len(result['partitions_values'])}")


if __name__ == "__main__":
    main()
