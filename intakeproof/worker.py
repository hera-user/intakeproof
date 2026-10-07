"""Trusted Agent37 worker. Reads only the request in its generated job directory."""

import base64
import json
from pathlib import Path

from engine import execute, json_bytes


def main():
    directory = Path(__file__).resolve().parent
    request = json.loads((directory / "request.json").read_text("utf-8"))
    raw = base64.b64decode(request["source_base64"], validate=True)
    result = execute(raw, request["recipe"], delimiter=request["delimiter"], decisions=request.get("decisions"),
                     mapping_approved=request.get("mapping_approved") is True, planner_receipt=request.get("planner_receipt"),
                     parent_run_id=request.get("parent_run_id"),
                     executor_receipt={"mode": "agent37_worker_unverified", "verified": False})
    (directory / "result.json").write_bytes(json_bytes(result))
    print("INTAKEPROOF_COMPLETED:" + request["job_id"])


if __name__ == "__main__":
    main()
