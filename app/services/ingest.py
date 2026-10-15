"""Ingest plane: single-event inserts and the COPY-based bulk loader.

Bulk loading uses Postgres COPY into a temp staging table and then one INSERT ..
SELECT: roughly 50x faster than row-by-row inserts, atomic (a bad row fails the whole
batch with the exact line number), and type-checked by the database itself.
"""
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text

from app.core import metrics
from app.core.db import SessionLocal, engine
from app.core.errors import APIError
from app.models.raw import DEPARTURE_TYPES

log = structlog.get_logger("ingest")

CSV_COLUMNS = (
    "patient_ref,event_type,department_id,event_time,severity,admission_source,bed_id,length_of_stay_hours"
)
MAX_FUTURE = timedelta(days=1)
CLAIM_TIMEOUT_SECONDS = 600

_dept_cache: tuple[float, set[int]] = (0.0, set())


def known_department_ids() -> set[int]:
    """Department IDs, cached for a minute so ingestion does not query for them per event."""
    global _dept_cache
    now = time.monotonic()
    if now - _dept_cache[0] > 60 or not _dept_cache[1]:
        with SessionLocal() as s:
            ids = {r[0] for r in s.execute(text("SELECT department_id FROM raw.departments"))}
        _dept_cache = (now, ids)
    return _dept_cache[1]


def invalidate_department_cache() -> None:
    global _dept_cache
    _dept_cache = (0.0, set())


def validate_event_semantics(event) -> None:
    """Rules a schema cannot express. Raises 422 (well-formed but unacceptable)."""
    problems = []
    if event.department_id not in known_department_ids():
        problems.append({"field": "department_id", "message": f"unknown department {event.department_id}"})
    if event.length_of_stay_hours is not None and event.event_type.value not in DEPARTURE_TYPES:
        problems.append({"field": "length_of_stay_hours", "message": "only allowed on discharge events"})
    if event.event_time > datetime.now(UTC) + MAX_FUTURE:
        problems.append({"field": "event_time", "message": "event_time is more than a day in the future"})
    if problems:
        raise APIError(422, "unprocessable", "Event failed semantic validation", details=problems)


_INSERT_EVENT = text(
    """
    INSERT INTO raw.hospital_events
      (patient_ref, event_type, department_id, event_time, severity, admission_source,
       bed_id, length_of_stay_hours, correlation_id, ingested_by_key)
    VALUES
      (:patient_ref, :event_type, :department_id, :event_time, :severity, :admission_source,
       :bed_id, :los, :correlation_id, :key_id)
    RETURNING event_id, ingested_at
    """
)


def insert_event(db, event, correlation_id: str, key_id: int):
    row = db.execute(
        _INSERT_EVENT,
        {
            "patient_ref": event.patient_ref,
            "event_type": event.event_type.value,
            "department_id": event.department_id,
            "event_time": event.event_time,
            "severity": event.severity,
            "admission_source": event.admission_source.value,
            "bed_id": event.bed_id,
            "los": event.length_of_stay_hours,
            "correlation_id": correlation_id,
            "key_id": key_id,
        },
    ).one()
    db.commit()
    metrics.record_ingested(1)
    return row.event_id, row.ingested_at


# ------------------------------------------------------------------- bulk load
def create_batch(db, source_uri: str, uploaded_by: str, correlation_id: str) -> uuid.UUID:
    batch_id = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO raw.ingest_batches (batch_id, source_uri, uploaded_by, status, correlation_id) "
            "VALUES (:id, :uri, :by, 'queued', :cid)"
        ),
        {"id": batch_id, "uri": source_uri, "by": uploaded_by, "cid": correlation_id},
    )
    db.commit()
    return batch_id


def mark_batch_failed(batch_id: str, detail: dict) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE raw.ingest_batches SET status = 'failed', finished_at = clock_timestamp(), "
                "error_detail = CAST(:d AS jsonb) WHERE batch_id = :id"
            ),
            {"d": json.dumps(detail), "id": batch_id},
        )


def load_csv_stream(raw_conn, fileobj, batch_id: str | None, correlation_id: str, key_id: int | None) -> int:
    """COPY a CSV (header + CSV_COLUMNS) into raw.hospital_events. Runs in the caller's
    transaction; returns rows inserted."""
    cur = raw_conn.cursor()
    cur.execute(
        """
        CREATE TEMP TABLE _stage (
            patient_ref varchar(32), event_type raw.event_type, department_id int,
            event_time timestamptz, severity smallint, admission_source raw.admission_source,
            bed_id varchar(16), length_of_stay_hours numeric(8,2)
        ) ON COMMIT DROP
        """
    )
    cur.copy_expert(f"COPY _stage ({CSV_COLUMNS}) FROM STDIN WITH (FORMAT csv, HEADER true)", fileobj)
    cur.execute(
        f"""
        INSERT INTO raw.hospital_events
          ({CSV_COLUMNS}, ingested_at, correlation_id, batch_id, ingested_by_key)
        SELECT {CSV_COLUMNS}, now(), %s::uuid, %s::uuid, %s FROM _stage
        """,
        (correlation_id, batch_id, key_id),
    )
    return cur.rowcount


def run_batch_load(batch_id: str, source_uri: str, correlation_id: str | None, key_id: int | None) -> int:
    """Worker-side bulk load: stream the object from storage straight into COPY."""
    from app.services import storage

    t0 = time.perf_counter()
    cid = correlation_id or str(uuid.uuid4())
    # Claim the batch. Unlike map and reduce tasks this one is NOT idempotent (a second COPY would
    # insert every row again), and the broker redelivers any task still running after the visibility
    # timeout. Only one delivery may win the claim; a loader that died mid-COPY rolled back, and its
    # claim is considered abandoned after CLAIM_TIMEOUT so the batch can be picked up again.
    with engine.begin() as conn:
        claimed = conn.execute(
            text(
                "UPDATE raw.ingest_batches SET status = 'loading', started_at = clock_timestamp() "
                "WHERE batch_id = :id AND (status = 'queued' OR "
                "(status = 'loading' AND started_at < clock_timestamp() - make_interval(secs => :ttl))) "
                "RETURNING batch_id"
            ),
            {"id": batch_id, "ttl": CLAIM_TIMEOUT_SECONDS},
        ).first()
    if claimed is None:
        log.warning("batch.duplicate_delivery_skipped", reason="already loading, completed or failed")
        return 0
    log.info("batch.loading", source_uri=source_uri)
    try:
        body = storage.open_object(source_uri)
        with engine.begin() as conn:
            rows = load_csv_stream(conn.connection, body, batch_id, cid, key_id)
            conn.execute(
                text(
                    "UPDATE raw.ingest_batches SET status = 'completed', row_count = :n, "
                    "finished_at = clock_timestamp() WHERE batch_id = :id"
                ),
                {"n": rows, "id": batch_id},
            )
    except Exception as exc:
        # Includes COPY's data errors (the message names the offending line). Not retryable.
        if _is_transient(exc):
            raise
        log.error("batch.failed", error=str(exc)[:500])
        mark_batch_failed(batch_id, {"code": "load_failed", "message": str(exc)[:500]})
        return 0
    elapsed = time.perf_counter() - t0
    metrics.record_ingested(rows)
    log.info("batch.completed", rows=rows, duration_s=round(elapsed, 2), rows_per_second=round(rows / max(elapsed, 1e-9)))
    return rows


def _is_transient(exc: Exception) -> bool:
    from redis.exceptions import RedisError
    from sqlalchemy.exc import OperationalError

    return isinstance(exc, (OperationalError, RedisError, ConnectionError, TimeoutError))
