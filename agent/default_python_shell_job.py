"""Placeholder script for AWS Glue Python Shell jobs.

Why this file exists:
    Glue's CreateJob API requires a ScriptLocation (an S3 URI pointing to a
    .py file) at the moment the job is created. You cannot create an empty
    job and upload the real script later. So when the agent scaffolds a new
    Glue job before any real code has been written for it, it still needs
    *something* to point ScriptLocation at. This file is that something: a
    no-op that prints an "ok" JSON blob and exits, so the job registers
    cleanly and the real script can be swapped in later.

How it gets used:
    setup_glue_job_prereqs.sh uploads this file to S3 once during setup
    (default key: scripts/default_python_shell_job.py under
    $GLUE_ASSETS_BUCKET) and exports GLUE_JOB_DEFAULT_SCRIPT_S3. The agent
    reads that env var (see agent/agent.py) and tells the LLM to use that
    URI as the default ScriptLocation whenever it creates a new Glue job.

If you remove this file you must also stop the agent from advertising a
default ScriptLocation, and require every Glue job creation to specify a
real script URI up front.
"""

import json
from datetime import datetime, timezone


def main() -> int:
    payload = {
        "status": "ok",
        "message": "Default Glue Python Shell job script executed.",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
