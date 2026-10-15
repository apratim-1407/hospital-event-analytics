import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import ApiKeyPrincipal, Principal, get_api_key, require_role
from app.core.errors import APIError
from app.core.security import Role
from app.schemas.common import ErrorEnvelope, decode_cursor, encode_cursor
from app.schemas.events import (
    BatchAccepted,
    BatchRequest,
    EventAccepted,
    EventIn,
    EventOut,
    EventPage,
    EventType,
)
from app.services import ingest, storage

log = structlog.get_logger("ingest")
router = APIRouter(prefix="/events", tags=["events"])


def _cid(request: Request) -> str:
    return request.scope["state"]["correlation_id"]


@router.post(
    "",
    response_model=EventAccepted,
    status_code=201,
    summary="Ingest one hospital event (API key)",
    responses={401: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}, 429: {"model": ErrorEnvelope}},
)
def ingest_event(
    event: EventIn,
    request: Request,
    key: ApiKeyPrincipal = Depends(get_api_key),
    db: Session = Depends(get_db),
):
    """Validates, stamps `ingested_at` and a `correlation_id`, and appends to the raw log.
    No analysis happens here; this path must stay fast and never touches the broker."""
    ingest.validate_event_semantics(event)
    cid = _cid(request)
    try:
        event_id, ingested_at = ingest.insert_event(db, event, cid, key.key_id)
    except Exception as exc:
        db.rollback()
        # A rejected insert (bad enum, FK) is the client's problem; anything else is ours.
        log.warning("ingest.rejected", error=type(exc).__name__, patient_ref=event.patient_ref)
        raise
    # The log line records that a patient event happened; it never identifies a person.
    log.info(
        "ingest.event_accepted",
        event_id=event_id,
        event_type=event.event_type.value,
        department_id=event.department_id,
        patient_ref=event.patient_ref,
    )
    return EventAccepted(event_id=event_id, correlation_id=uuid.UUID(cid), ingested_at=ingested_at)


@router.post(
    "/batch",
    response_model=BatchAccepted,
    status_code=202,
    summary="Bulk ingest a CSV staged in object storage (API key)",
    responses={401: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}, 503: {"model": ErrorEnvelope}},
)
def ingest_batch(
    body: BatchRequest,
    request: Request,
    key: ApiKeyPrincipal = Depends(get_api_key),
    db: Session = Depends(get_db),
):
    """Creates a batch record and hands the load to a worker (`COPY`, atomic). Poll
    `/events/batch/{batch_id}` for status."""
    try:
        storage.parse_s3_uri(body.source_uri)
        exists = storage.object_exists(body.source_uri)
    except ValueError as exc:
        raise APIError(422, "unprocessable", str(exc))
    except Exception as exc:
        log.error("batch.storage_unreachable", error=type(exc).__name__)
        raise APIError(503, "service_unavailable", "Object storage is unreachable")
    if not exists:
        raise APIError(422, "unprocessable", "source_uri does not exist in object storage")

    cid = _cid(request)
    batch_id = ingest.create_batch(db, body.source_uri, f"apikey:{key.key_id}", cid)
    from app.pipeline.tasks import load_batch

    try:
        load_batch.apply_async(
            args=[str(batch_id), body.source_uri, cid, key.key_id],
            retry=True,
            retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5},
        )
    except Exception:
        ingest.mark_batch_failed(str(batch_id), {"code": "broker_unavailable", "message": "queue unreachable"})
        raise APIError(503, "service_unavailable", "The processing queue is unavailable; nothing was loaded", {"Retry-After": "10"})
    log.info("batch.accepted", batch_id=str(batch_id), source_uri=body.source_uri)
    return BatchAccepted(batch_id=batch_id, status="queued", poll_url=f"/api/v1/events/batch/{batch_id}")


@router.get(
    "/batch/{batch_id}",
    summary="Status of a bulk load (API key)",
    responses={404: {"model": ErrorEnvelope}},
)
def batch_status(batch_id: uuid.UUID, _: ApiKeyPrincipal = Depends(get_api_key), db: Session = Depends(get_db)):
    row = db.execute(
        text(
            "SELECT batch_id, source_uri, status, row_count, error_detail, created_at, started_at, finished_at "
            "FROM raw.ingest_batches WHERE batch_id = :id"
        ),
        {"id": batch_id},
    ).mappings().one_or_none()
    if row is None:
        raise APIError(404, "not_found", f"Batch {batch_id} not found")
    out = dict(row)
    if out["started_at"] and out["finished_at"] and out["row_count"]:
        secs = (out["finished_at"] - out["started_at"]).total_seconds()
        out["rows_per_second"] = round(out["row_count"] / secs) if secs > 0 else None
    return out


@router.get(
    "",
    response_model=EventPage,
    summary="Query raw events with filters (any role)",
)
def list_events(
    _: Principal = Depends(require_role(Role.VIEWER)),
    db: Session = Depends(get_db),
    start: datetime | None = Query(None, description="event_time >= start"),
    end: datetime | None = Query(None, description="event_time < end"),
    department_id: int | None = Query(None, ge=1),
    event_type: EventType | None = None,
    patient_ref: str | None = Query(None, max_length=32),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
):
    """Keyset-paginated over (event_time, event_id). Filter by time range for best speed:
    that prunes the monthly partitions."""
    conds, params = [], {}
    if start:
        conds.append("event_time >= :start")
        params["start"] = start
    if end:
        conds.append("event_time < :end")
        params["end"] = end
    if department_id:
        conds.append("department_id = :dept")
        params["dept"] = department_id
    if event_type:
        conds.append("event_type = :etype")
        params["etype"] = event_type.value
    if patient_ref:
        conds.append("patient_ref = :pref")
        params["pref"] = patient_ref
    c = decode_cursor(cursor)
    if c:
        conds.append("(event_time, event_id) > (CAST(:ct AS timestamptz), CAST(:ci AS bigint))")
        params["ct"], params["ci"] = c["t"], c["i"]
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = db.execute(
        text(
            f"""
            SELECT event_id, patient_ref, event_type::text AS event_type, department_id, event_time,
                   severity, admission_source::text AS admission_source, bed_id,
                   length_of_stay_hours::float8 AS length_of_stay_hours, ingested_at, correlation_id, batch_id
            FROM raw.hospital_events {where}
            ORDER BY event_time, event_id
            LIMIT :lim
            """
        ),
        {**params, "lim": limit + 1},
    ).mappings().all()
    db.close()  # free the pooled connection now, not at request teardown
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor({"t": last["event_time"].isoformat(), "i": last["event_id"]})
    return EventPage(data=[EventOut(**r) for r in rows], next_cursor=next_cursor)
