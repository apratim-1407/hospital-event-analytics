"""Job lifecycle on the API side: validate, record, enqueue, report.

The API never runs analysis and never addresses a worker. It writes a `queued` row,
puts a message on the broker, and returns 202: "I have accepted responsibility for
this work but have not done it".
"""
import uuid
from datetime import date

import structlog
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import engine
from app.core.errors import APIError
from app.core.redis import guarded
from app.models.analytics import AnalysisJob
from app.pipeline import shuffle
from app.pipeline.partition import fetch_data_bounds, make_plan, plan_partitions
from app.schemas.jobs import JobCreate, JobOut

log = structlog.get_logger("jobs")

FINAL_STATUSES = {"completed", "failed", "partial", "cancelled"}


def _resolve_departments(db: Session, requested: list[int] | None) -> list[int]:
    known = [r[0] for r in db.execute(text("SELECT department_id FROM raw.departments ORDER BY department_id"))]
    if not requested:
        return known
    unknown = sorted(set(requested) - set(known))
    if unknown:
        raise APIError(422, "unprocessable", f"Unknown department IDs: {unknown}")
    return sorted(set(requested))


def _validate_trace_ids(ids: list[str]) -> list[str]:
    out = []
    for value in ids:
        try:
            out.append(str(uuid.UUID(value)))
        except ValueError:
            raise APIError(422, "unprocessable", f"trace_correlation_ids contains a non-UUID value: {value!r}")
    return out


def create_job(db: Session, body: JobCreate, requested_by: int, correlation_id: str) -> AnalysisJob:
    span = (body.end_date - body.start_date).days + 1
    if span > settings.max_job_span_days:
        raise APIError(422, "unprocessable", f"Date range is {span} days; the maximum is {settings.max_job_span_days}")

    dept_ids = _resolve_departments(db, body.departments)
    trace_ids = _validate_trace_ids(body.trace_correlation_ids)

    with engine.connect() as conn:
        data_start, data_end = fetch_data_bounds(conn)
    plan = make_plan(body.job_type.value, body.start_date, body.end_date, data_start, data_end)
    partitions_total = len(plan_partitions(plan, dept_ids))

    job = AnalysisJob(
        job_id=uuid.uuid4(),
        job_type=body.job_type.value,
        status="queued",
        params={
            "start_date": body.start_date.isoformat(),
            "end_date": body.end_date.isoformat(),
            "departments": dept_ids,
            "trace_correlation_ids": trace_ids,
            "data_start": data_start.isoformat() if data_start else None,
            "data_end": data_end.isoformat() if data_end else None,
            "range_start": plan.range_start.isoformat(),
            "range_end": plan.range_end.isoformat(),
        },
        requested_by=requested_by,
        correlation_id=uuid.UUID(correlation_id),
        partitions_total=partitions_total,
    )
    db.add(job)
    db.commit()
    log.info(
        "job.accepted",
        job_id=str(job.job_id),
        job_type=job.job_type,
        partitions_total=partitions_total,
        start_date=body.start_date.isoformat(),
        end_date=body.end_date.isoformat(),
    )
    enqueue(job, correlation_id, db)
    return job


def enqueue(job: AnalysisJob, correlation_id: str, db: Session) -> None:
    """Put the job on the broker. If the broker is down, say so plainly (503) instead of
    hanging or returning a misleading success. Ingestion is unaffected: the write path
    does not touch the broker."""
    from app.pipeline.tasks import run_job

    try:
        run_job.apply_async(
            args=[str(job.job_id), correlation_id],
            retry=True,
            retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5},
        )
    except Exception as exc:
        log.error("job.enqueue_failed", job_id=str(job.job_id), error=type(exc).__name__)
        job.status = "failed"
        job.error_detail = {"code": "broker_unavailable", "message": "The job queue is unreachable; nothing was started"}
        db.commit()
        raise APIError(
            503,
            "service_unavailable",
            "The analytics queue is currently unavailable. Event ingestion is unaffected; please retry shortly.",
            {"Retry-After": "10"},
        )


def live_progress(job: AnalysisJob) -> tuple[int, str | None, dict | None]:
    """(partitions_completed, status override, error override) for a job still in flight.
    Progress lives in Redis while a job runs, because one DB row updated thousands of
    times would be a hot-row bottleneck."""
    if job.status in FINAL_STATUSES:
        return job.partitions_completed, None, None
    done = guarded(lambda r: int(r.scard(f"job:{job.job_id}:done")), default=None, db=3)
    note = guarded(lambda r: shuffle.get_failure_note(str(job.job_id)), default=None, db=3)
    if note:  # the reducer gave up and the database was unreachable at the time
        return (done or job.partitions_completed), "failed", note
    return (done if done is not None else job.partitions_completed), None, None


def to_out(job: AnalysisJob) -> JobOut:
    done, status_override, error_override = live_progress(job)
    total = job.partitions_total or 0
    pct = 100.0 if (job.status == "completed" or (total == 0 and job.status in FINAL_STATUSES)) else (
        round(100.0 * done / total, 1) if total else 0.0
    )
    return JobOut(
        job_id=job.job_id,
        job_type=job.job_type,
        status=status_override or job.status,
        params=job.params,
        requested_by=job.requested_by,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        duration_seconds=float(job.duration_seconds) if job.duration_seconds is not None else None,
        events_processed=job.events_processed,
        partitions_total=total,
        partitions_completed=done if job.status not in FINAL_STATUSES else job.partitions_completed,
        progress_pct=pct,
        error_detail=error_override or job.error_detail,
        poll_url=f"/api/v1/jobs/{job.job_id}",
    )


def get_job_or_404(db: Session, job_id: uuid.UUID) -> AnalysisJob:
    job = db.get(AnalysisJob, job_id)
    if job is None:
        raise APIError(404, "not_found", f"Job {job_id} not found")
    return job


def cancel_job(db: Session, job: AnalysisJob) -> AnalysisJob:
    if job.status in FINAL_STATUSES:
        raise APIError(409, "conflict", f"Job is already {job.status}")
    shuffle.set_cancelled(str(job.job_id))
    job.status = "cancelled"
    job.error_detail = {"code": "cancelled", "message": "Cancelled by user"}
    db.execute(
        text(
            "UPDATE analytics.analysis_jobs SET finished_at = clock_timestamp(), "
            "duration_seconds = round(EXTRACT(EPOCH FROM (clock_timestamp() - COALESCE(started_at, created_at)))::numeric, 3) "
            "WHERE job_id = :id"
        ),
        {"id": job.job_id},
    )
    db.commit()
    db.refresh(job)
    log.info("job.cancelled", job_id=str(job.job_id))
    return job


def clone_params_for_rerun(job: AnalysisJob) -> JobCreate:
    p = job.params
    return JobCreate(
        job_type=job.job_type,
        start_date=date.fromisoformat(p["start_date"]),
        end_date=date.fromisoformat(p["end_date"]),
        departments=p.get("departments"),
        trace_correlation_ids=p.get("trace_correlation_ids", []),
    )


def list_jobs(db: Session, status: str | None, limit: int, cursor: dict | None) -> tuple[list[AnalysisJob], dict | None]:
    stmt = select(AnalysisJob).order_by(AnalysisJob.created_at.desc(), AnalysisJob.job_id.desc())
    if status:
        stmt = stmt.where(AnalysisJob.status == status)
    if cursor:
        stmt = stmt.where(
            text("(created_at, job_id) < (CAST(:c AS timestamptz), CAST(:j AS uuid))").bindparams(
                c=cursor["created_at"], j=cursor["job_id"]
            )
        )
    rows = list(db.execute(stmt.limit(limit + 1)).scalars())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = {"created_at": last.created_at.isoformat(), "job_id": str(last.job_id)}
    return rows, next_cursor
