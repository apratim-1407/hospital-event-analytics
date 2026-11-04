"""Submit a full analysis through the public API and wait for it to finish.

Used by deploy/seed-demo.sh so a freshly deployed system has results to show. Reads the API
address and admin login from the environment (API_BASE_URL, SEED_ADMIN_EMAIL, SEED_ADMIN_PASSWORD).

    python -m scripts.run_analysis --start 2023-01-01 --end 2025-12-31
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/") + "/api/v1"


def call(method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, method=method, headers=headers, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} failed: {e.code} {e.read().decode()[:300]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    token = call("POST", "/auth/login", {"email": os.environ["SEED_ADMIN_EMAIL"], "password": os.environ["SEED_ADMIN_PASSWORD"]})["access_token"]
    job = call("POST", "/jobs", {"job_type": "full_analysis", "start_date": args.start, "end_date": args.end}, token)
    print(f"submitted job {job['job_id']} ({job['partitions_total']} partitions)", flush=True)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        j = call("GET", f"/jobs/{job['job_id']}", token=token)
        print(f"  {j['status']}: {j.get('partitions_completed')}/{j.get('partitions_total')}", flush=True)
        if j["status"] in {"completed", "partial", "failed", "cancelled"}:
            print(json.dumps({"status": j["status"], "events_processed": j.get("events_processed"), "duration_seconds": j.get("duration_seconds")}))
            sys.exit(0 if j["status"] == "completed" else 1)
        time.sleep(3)
    sys.exit("timed out waiting for the analysis")


if __name__ == "__main__":
    main()
