#!/usr/bin/env python
"""Push goldens from `eval/goldens/` to Phoenix as a versioned dataset.

A Phoenix-side dataset is the input to `phoenix.experiments.run_experiment`,
which is what gives us the comparison-across-runs UI (PR vs main, prompt v1
vs v0, etc.). Git remains the source of truth for the goldens themselves;
this script is the bridge.

Usage (operator runs once per dataset change):

    export PHOENIX_ENDPOINT=http://localhost:6006   # default
    uv run --group dev python scripts/upload_dataset.py

Idempotent on dataset name: if the named dataset already exists, this
exits without re-uploading. To append a new version, pass `--append`.

Env vars:
  PHOENIX_ENDPOINT     where to POST. Defaults to http://localhost:6006
                       (set up a port-forward to the Phoenix ALB).
  PHOENIX_API_KEY      optional; only needed if Phoenix auth is enabled.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("upload_dataset")

DEFAULT_DATASET_NAME = "data-analyst-goldens"
DEFAULT_GOLDENS_DIR = Path(__file__).resolve().parent.parent / "eval" / "goldens"


def main() -> int:
    args = _parse_args()

    goldens = _load_goldens(args.goldens_dir)
    if not goldens:
        log.error("No goldens found under %s", args.goldens_dir)
        return 2
    log.info("Loaded %d goldens from %s", len(goldens), args.goldens_dir)

    inputs, outputs, metadata = _to_phoenix_rows(goldens)

    endpoint = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006")
    api_key = os.environ.get("PHOENIX_API_KEY") or None
    client = _client(endpoint, api_key)

    existing = _try_get_dataset(client, args.name)
    if existing is not None and not args.append:
        log.info(
            "Dataset %r already exists (id=%s). Pass --append to add a "
            "new version. Exiting without changes.",
            args.name,
            getattr(existing, "id", "?"),
        )
        return 0

    if existing is not None and args.append:
        log.info("Appending %d examples to dataset %r as a new version", len(inputs), args.name)
        dataset = client.datasets.add_examples_to_dataset(
            dataset=args.name,
            inputs=inputs,
            outputs=outputs,
            metadata=metadata,
        )
    else:
        log.info("Creating dataset %r with %d examples", args.name, len(inputs))
        dataset = client.datasets.create_dataset(
            name=args.name,
            inputs=inputs,
            outputs=outputs,
            metadata=metadata,
            dataset_description=(
                "Data analyst agent goldens (Iris + Glue + Athena smoke). "
                "Edited in git under eval/goldens/."
            ),
        )

    log.info("Dataset id: %s", getattr(dataset, "id", "?"))
    log.info(
        "Open in Phoenix UI under Datasets → %s. "
        "Use scripts/upload_dataset.py --append to add new examples later.",
        args.name,
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--name",
        default=DEFAULT_DATASET_NAME,
        help=f"Phoenix dataset name. Default: {DEFAULT_DATASET_NAME}",
    )
    parser.add_argument(
        "--goldens-dir",
        type=Path,
        default=DEFAULT_GOLDENS_DIR,
        help=f"Directory containing *.json goldens. Default: {DEFAULT_GOLDENS_DIR}",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append as a new dataset version even if the dataset already exists.",
    )
    return parser.parse_args()


def _load_goldens(root: Path) -> list[dict[str, Any]]:
    goldens: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        content = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(content, list):
            goldens.extend(content)
        elif isinstance(content, dict):
            goldens.append(content)
        else:
            log.warning("Skipping %s: unexpected top-level type %s", path, type(content))
    return goldens


def _to_phoenix_rows(
    goldens: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Project our golden shape into Phoenix's input/output/metadata triplets."""
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    for g in goldens:
        inputs.append({"prompt": g["input"]})
        outputs.append({"expected_substrings": g.get("expected_answer_contains", [])})
        metadata.append({
            "golden_id": g["id"],
            "tags": g.get("tags", []),
            "context": g.get("context", []),
        })
    return inputs, outputs, metadata


def _client(endpoint: str, api_key: str | None):
    from phoenix.client import Client

    log.info("Phoenix endpoint: %s%s", endpoint, " (with api key)" if api_key else "")
    return Client(base_url=endpoint, api_key=api_key)


def _try_get_dataset(client, name: str):
    try:
        return client.datasets.get_dataset(dataset=name)
    except Exception as e:  # noqa: BLE001
        # Phoenix raises a typed not-found that varies across versions;
        # accept anything that looks like a miss as "doesn't exist".
        log.debug("get_dataset(%r) raised %s — treating as not-found", name, e)
        return None


if __name__ == "__main__":
    sys.exit(main())
