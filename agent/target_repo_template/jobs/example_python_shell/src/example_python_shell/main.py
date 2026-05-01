"""
Reference Python Shell Glue job.

All business logic goes here. `entrypoint.py` is a bootloader — it imports
this module's `main()` and calls it. Keep it that way so the entrypoint
doesn't change on every logic tweak.

Glue passes arguments on the command line. Parse them with argparse, using
the same keys declared in `default_arguments` in `glue-jobs.yaml`.
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    print(f"example_python_shell starting, output_path={args.output_path}")

    # TODO: replace with real logic.
    #   - Read input (from Athena/S3/wherever).
    #   - Transform.
    #   - Write to args.output_path.

    print("example_python_shell done")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output_path", required=True, help="S3 URI to write to")
    # Glue often injects extra args the job doesn't know about; ignore unknowns.
    parsed, _ = parser.parse_known_args(argv)
    return parsed
