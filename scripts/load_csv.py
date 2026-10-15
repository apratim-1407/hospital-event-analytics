"""Load a generated dataset into Postgres.

Direct mode (default): COPY straight from the file, the fast path for local runs.

    python -m scripts.load_csv data/small.csv --truncate

Storage mode: the path the deployed system uses. Upload the CSV to object storage, ask
the API to bulk-load it (POST /events/batch, API key), and poll the batch to completion.

    python -m scripts.load_csv data/small.csv --via-storage --api-key hea_...
"""
import argparse
import json
import os
import time
import urllib.request
import uuid
from pathlib import Path

from sqlalchemy import text

from app.core.db import engine
from app.core.logging import configure_logging
from app.core.redis import get_redis
from app.services import ingest

configure_logging()


def _copy_csv(raw_conn, sql: str, path: Path) -> None:
    with path.open("rb") as fh:
        raw_conn.cursor().copy_expert(sql, fh)


def reset(conn) -> None:
    """Dev reset. raw.hospital_events is append-only by trigger (UPDATE/DELETE are refused),
    so the only way to clear it is TRUNCATE, which is deliberate and never reachable via the API."""
    conn.execute(
        text(
            "TRUNCATE analytics.admission_metrics, analytics.resource_metrics, analytics.anomaly_windows, "
            "analytics.analysis_jobs, raw.ingest_batches, raw.hospital_events RESTART IDENTITY"
        )
    )
    conn.execute(text("DELETE FROM raw.beds"))


def load_reference(raw_conn, departments_csv: Path, beds_csv: Path) -> None:
    cur = raw_conn.cursor()
    cur.execute(
        "CREATE TEMP TABLE _dept (department_id int, name text, dept_type text, bed_capacity int, staff_count int) ON COMMIT DROP"
    )
    _copy_csv(raw_conn, "COPY _dept FROM STDIN WITH (FORMAT csv, HEADER true)", departments_csv)
    cur.execute(
        """
        INSERT INTO raw.departments (department_id, name, dept_type, bed_capacity, staff_count)
        SELECT department_id, name, dept_type, bed_capacity, staff_count FROM _dept
        ON CONFLICT (department_id) DO UPDATE SET
            name = EXCLUDED.name, dept_type = EXCLUDED.dept_type,
            bed_capacity = EXCLUDED.bed_capacity, staff_count = EXCLUDED.staff_count
        """
    )
    cur.execute("DELETE FROM raw.beds")
    _copy_csv(raw_conn, "COPY raw.beds (bed_id, department_id, bed_type) FROM STDIN WITH (FORMAT csv, HEADER true)", beds_csv)


def load_direct(events_csv: Path, truncate: bool) -> dict:
    stem = events_csv.with_suffix("")
    departments_csv = events_csv.with_name(stem.name + "_departments.csv")
    beds_csv = events_csv.with_name(stem.name + "_beds.csv")
    batch_id = str(uuid.uuid4())
    cid = str(uuid.uuid4())

    with engine.begin() as conn:
        if truncate:
            reset(conn)
        if departments_csv.exists() and beds_csv.exists():
            load_reference(conn.connection, departments_csv, beds_csv)
        conn.execute(
            text(
                "INSERT INTO raw.ingest_batches (batch_id, source_uri, uploaded_by, status, correlation_id, started_at) "
                "VALUES (:b, :u, 'scripts.load_csv', 'loading', :c, clock_timestamp())"
            ),
            {"b": batch_id, "u": f"file://{events_csv}", "c": cid},
        )

    t0 = time.perf_counter()
    with engine.begin() as conn, events_csv.open("rb") as fh:
        rows = ingest.load_csv_stream(conn.connection, fh, batch_id, cid, None)
        conn.execute(
            text(
                "UPDATE raw.ingest_batches SET status = 'completed', row_count = :n, finished_at = clock_timestamp() "
                "WHERE batch_id = :b"
            ),
            {"n": rows, "b": batch_id},
        )
    elapsed = time.perf_counter() - t0

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("ANALYZE raw.hospital_events"))
    ingest.invalidate_department_cache()
    try:
        get_redis().incr("ac:version")  # drop any cached analytics
    except Exception:
        pass
    return {"rows": rows, "seconds": round(elapsed, 2), "rows_per_second": round(rows / max(elapsed, 1e-9))}


def _api(method: str, url: str, api_key: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None)
    req.add_header("X-API-Key", api_key)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def load_via_storage(events_csv: Path, api_url: str, api_key: str) -> dict:
    from app.services import storage

    key = f"uploads/{events_csv.name}"
    uri = storage.upload_file(str(events_csv), key)
    print(f"uploaded {events_csv.name} -> {uri}")
    accepted = _api("POST", f"{api_url}/api/v1/events/batch", api_key, {"source_uri": uri})
    print(f"batch accepted: {accepted['batch_id']}")
    while True:
        status = _api("GET", f"{api_url}{accepted['poll_url']}", api_key)
        if status["status"] in ("completed", "failed"):
            return status
        time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("events_csv", type=Path)
    ap.add_argument("--truncate", action="store_true", help="clear raw events and all analytics first (dev)")
    ap.add_argument("--via-storage", action="store_true", help="upload to object storage and use POST /events/batch")
    ap.add_argument("--api-url", default=os.environ.get("API_BASE_URL", "http://localhost:8000"))
    ap.add_argument("--api-key", default=os.environ.get("SEED_INGEST_API_KEY", ""))
    args = ap.parse_args()

    if args.via_storage:
        print(json.dumps(load_via_storage(args.events_csv, args.api_url, args.api_key), indent=2, default=str))
    else:
        print(json.dumps(load_direct(args.events_csv, args.truncate)))


if __name__ == "__main__":
    main()
