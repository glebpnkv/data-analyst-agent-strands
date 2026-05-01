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
