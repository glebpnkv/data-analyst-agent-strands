"""Build a Windows wheelhouse zip for the corp-laptop Chainlit setup.

Why this exists: the corp laptop's package mirror (Nexus) blocks
chainlit. Rather than fight that, we side-load — pre-download every
wheel chainlit needs into a folder, zip the folder, push it to S3.
On the corp laptop you pull it down and `pip install --no-index
--find-links=wheels/`.

Why frontend-only: pyproject.toml lists agent-side deps too
(strands-agents, bedrock-agentcore, mcp, fastapi, uvicorn, etc.).
None of those run on the corp laptop — the AgentCore Runtime
container has them. The corp laptop only needs what chainlit
itself + the host-side Lambda deployer + plotly rendering need:

  - chainlit                — chat UI + data layer
  - boto3                   — AgentCore invoke + S3 + Lambda + IAM
  - aiosqlite               — async SQLite driver for the data layer
  - sqlalchemy              — what SQLAlchemyDataLayer uses internally
  - greenlet                — sqlalchemy's async-compat shim
  - plotly==5.22.0          — pinned to match the sandbox's plotly
                              version so figure JSON round-trips
  - python-dotenv           — read .env files (used by chainlit)

Note: `bedrock-agentcore` (the Python SDK) is intentionally excluded
— the frontend reaches AgentCore via `boto3.client("bedrock-agentcore")`,
which boto3 supports natively on recent versions. No SDK needed.

Three-step build because pip's `--platform` flag interacts badly with
sdist-only packages:

  1. Build wheels locally for the handful of chainlit deps that ship
     ONLY as sdists on PyPI (literalai, syncer, cuid). They're pure
     Python, so the resulting `py3-none-any.whl` runs anywhere.
  2. Run `pip download --platform win_amd64 --only-binary=:all:` for
     the entire frontend dep tree, pointing `--find-links` at the
     locally-built wheels so pip's resolver is satisfied without
     trying (and failing) to fetch wheels for the sdist-only ones.
  3. Zip and push to S3.

Output:
  s3://hackathon-da-raw-<acct>-us-east-1/wheels/frontend-wheels.zip

Usage:
  AWS_PROFILE=hackathon uv run python scripts/hackathon_build_wheelhouse.py

Override the target Python with --python-version if your corp box
differs from the assumed 3.12.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import boto3

REGION = "us-east-1"

# Frontend-only top-level deps. Match the floors in pyproject.toml's
# main [project.dependencies] block where we've pinned (plotly is the
# only hard pin; everything else is a >= floor pip resolves at
# download time).
FRONTEND_REQUIREMENTS: list[str] = [
    "chainlit>=2.11.1",
    "boto3>=1.42.54",
    "aiosqlite>=0.20.0",
    "sqlalchemy>=2.0.49",
    "greenlet>=3.5.0",
    "plotly==5.22.0",
    "python-dotenv>=1.1.1",
]

# Transitive deps that ship as sdist-only on PyPI (no wheels at all,
# for any platform). Pure-Python — building locally produces a
# universal `py3-none-any.whl` that runs on Windows just fine. Pin
# the versions to match what chainlit currently pulls in; bump if
# chainlit's lockstep changes.
SDIST_ONLY_PINS: list[str] = [
    "literalai==0.1.201",  # chainlit hard-pins this one
    "syncer==2.0.3",
    "cuid==0.4",
]

# What pip considers a Mac-platform wheel — used to drop accidentally-
# downloaded macOS wheels if the build leaks any. (`--platform win_amd64`
# generally prevents this, but belt-and-braces.)
_MAC_WHEEL_MARKERS = ("macosx", "darwin")


def _bucket_name(account: str) -> str:
    return f"hackathon-da-raw-{account}-{REGION}"


def _run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _make_py312_venv(parent: Path) -> Path:
    """Create a fresh Python 3.12 venv with the latest pip, return its python.

    Two reasons we need a real Py3.12 venv on the host:
      - The `pip download --platform win_amd64` resolver evaluates
        environment markers (e.g. `python_version >= "3.13"`) against
        the running interpreter, so we need to actually BE on 3.12.
      - System Mac Python may be 3.13, which trips chainlit's
        `audioop-lts; python_version >= "3.13"` marker and tries to
        download a package that doesn't exist on PyPI.

    uv pip can spin one up in ~2s.
    """
    venv = parent / "venv-3.12"
    _run(["uv", "venv", str(venv), "--python", "3.12"])
    py = venv / "bin" / "python"
    # Ensure we have a recent pip — pre-25 versions also have the marker
    # evaluation bug above for `--platform`.
    _run([str(py), "-m", "ensurepip", "--upgrade"])
    _run([str(py), "-m", "pip", "install", "--upgrade", "pip"])
    return py


def _build_sdist_only_wheels(py: Path, wheels_dir: Path) -> None:
    """Build py3-none-any wheels for the sdist-only deps."""
    print(f"== Building sdist-only deps locally ==")
    _run([
        str(py), "-m", "pip", "wheel",
        "--no-deps",
        "--wheel-dir", str(wheels_dir),
        *SDIST_ONLY_PINS,
    ])


def _download_windows_wheels(py: Path, wheels_dir: Path, python_version: str) -> None:
    """Resolve + download the full frontend dep tree as Windows wheels.

    `--find-links wheels_dir` lets pip see the locally-built sdist
    wheels and skip them. `--only-binary=:all:` makes the download
    fail fast if anything else accidentally still needs an sdist.
    """
    print(f"== Downloading Windows wheels (python={python_version}) ==")
    _run([
        str(py), "-m", "pip", "download",
        "--platform", "win_amd64",
        "--python-version", python_version,
        "--only-binary=:all:",
        "--find-links", str(wheels_dir),
        "--dest", str(wheels_dir),
        *FRONTEND_REQUIREMENTS,
    ])


def _drop_mac_wheels(wheels_dir: Path) -> None:
    """Remove any macOS-platform wheels that snuck in.

    Shouldn't happen with `--platform win_amd64`, but if pip ever
    decides to cache a Mac wheel (e.g. one already present from a
    previous run), we don't want it shipped to Windows.
    """
    removed = 0
    for f in wheels_dir.iterdir():
        if f.suffix != ".whl":
            continue
        name = f.name.lower()
        if any(m in name for m in _MAC_WHEEL_MARKERS):
            print(f"  - dropping {f.name} (mac wheel)")
            f.unlink()
            removed += 1
    if removed:
        print(f"== Dropped {removed} mac wheels ==")


def _zip_dir(src_dir: Path, zip_path: Path) -> None:
    """Zip everything under `src_dir` into `zip_path` (flat, no parent dir)."""
    files = sorted(src_dir.iterdir())
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, arcname=f.name)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--python-version", default="3.12",
                   help="Target Python version on the corp laptop (default: 3.12).")
    p.add_argument("--key", default="wheels/frontend-wheels.zip",
                   help="S3 key for the uploaded zip (default: wheels/frontend-wheels.zip).")
    p.add_argument("--keep-tmp", action="store_true",
                   help="Don't delete the temporary build dir on exit (debugging).")
    args = p.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="wheelhouse-"))
    print(f"-- working in {tmp} --")
    try:
        wheels_dir = tmp / "wheels"
        wheels_dir.mkdir()

        # Stash a manifest in the wheels dir so the corp-laptop side
        # can confirm what they're getting. Lands inside the zip.
        (wheels_dir / "requirements-frontend.txt").write_text(
            "\n".join(FRONTEND_REQUIREMENTS) + "\n"
        )

        py = _make_py312_venv(tmp)
        _build_sdist_only_wheels(py, wheels_dir)
        _download_windows_wheels(py, wheels_dir, args.python_version)
        _drop_mac_wheels(wheels_dir)

        wheel_count = sum(1 for f in wheels_dir.iterdir() if f.suffix == ".whl")
        print(f"\n== Final wheel count: {wheel_count} ==")

        zip_path = tmp / "frontend-wheels.zip"
        _zip_dir(wheels_dir, zip_path)
        zip_size = zip_path.stat().st_size
        print(f"== Zipped to {zip_size:,} bytes ==")

        # Upload.
        session = boto3.Session(region_name=REGION)
        sts = session.client("sts")
        s3 = session.client("s3")
        account = sts.get_caller_identity()["Account"]
        bucket = _bucket_name(account)
        s3.put_object(
            Bucket=bucket, Key=args.key, Body=zip_path.read_bytes(),
            ContentType="application/zip",
        )
        print(f"\n  + s3://{bucket}/{args.key}")
        print(f"\n  Pull on the corp laptop with:")
        print(f"    aws s3 cp s3://{bucket}/{args.key} .")
    finally:
        if not args.keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
