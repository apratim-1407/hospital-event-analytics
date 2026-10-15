"""Celery tasks: thin wrappers that add retry policy, logging and context around the
pure pipeline functions."""
import time
import uuid

import structlog
from celery import chord, group
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.analytics import AnalysisJob
from app.pipeline import map_tasks, shuffle
from app.pipeline.celery_app import celery_app
from app.pipeline.partition import plan_partitions
from app.pipeline.reduce import (
    BarrierNotReady,
    job_plan_from_params,
    mark_job_failed,
    run_reduce,
)

log = structlog.get_logger("pipeline")

# Errors that are worth retrying: the dependency may come back.
TRANSIENT = (OperationalError, RedisError, ConnectionError, TimeoutError)


def _backoff(retries: int) -> int:
    """1s, 2s, 4s, 8s, 16s: exponential, capped by the retry limit."""
    return min(2**retries, 30)


def _bind(correlation_id: str | None, **fields) -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id, **fields)


# ------------------------------------------------------------------ coordinator
@celery_app.task(bind=True, name="pipeline.run_job", ignore_result=True, max_retries=5, acks_late=True)
def run_job(self, job_id: str, correlation_id: str | None = None):
    """Coordinator: mark the job running, partition it, dispatch the map group with the
    reducer as the chord callback."""
    _bind(correlation_id, job_id=job_id, task="coordinator")
    try:
        with SessionLocal() as session:
            job = session.get(AnalysisJob, uuid.UUID(job_id))
            if job is None:
                log.error("job.not_found")
                return
            if job.status in ("completed", "cancelled", "failed", "partial"):
                log.info("job.dispatch_skipped", status=job.status)
                return
            job_type, params = job.job_type, dict(job.params)
            trace_ids = list(params.get("trace_correlation_ids") or [])

        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE analytics.analysis_jobs "
                    "SET status = 'running', started_at = COALESCE(started_at, clock_timestamp()) "
                    "WHERE job_id = :id AND status IN ('queued', 'running')"
                ),
                {"id": job_id},
            )

        plan, dept_ids = job_plan_from_params(job_type, params)
        partitions = plan_partitions(plan, dept_ids)
        log.info(
            "job.started",
            job_type=job_type,
            partitions=len(partitions),
            range_start=plan.range_start.isoformat(),
            range_end=plan.range_end.isoformat(),
            departments=len(dept_ids),
        )

        if not partitions:
            # No data in the requested window: nothing to map, finish immediately.
            run_reduce(job_id)
            return

        header = group(
            map_partition.s(job_id, p.day.isoformat(), p.department_id, trace_ids, correlation_id) for p in partitions
        )
        chord(header)(reduce_job.s(job_id, correlation_id))
        log.info("job.dispatched", partitions=len(partitions))
    except TRANSIENT as exc:
        if self.request.retries >= self.max_retries:
            mark_job_failed(job_id, {"code": "dispatch_failed", "message": str(exc)[:300]})
            log.error("job.dispatch_failed", error=str(exc))
            return
        log.warning("job.dispatch_retry", error=str(exc), retry=self.request.retries + 1)
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries))


# ------------------------------------------------------------------------- map
@celery_app.task(bind=True, name="pipeline.map_partition", max_retries=settings.map_max_retries, acks_late=True)
def map_partition(self, job_id: str, day_iso: str, department_id: int, trace_ids: list[str], correlation_id: str | None):
    partition = map_tasks.partition_from_args(day_iso, department_id)
    _bind(correlation_id, job_id=job_id, task="map", partition=f"{day_iso}/{department_id}")
    delivery = getattr(self.request, "delivery_info", None) or {}
    redelivered = bool(delivery.get("redelivered"))
    if redelivered:
        # The previous worker died holding this task; the broker handed it to us.
        log.warning("map_task.redelivered", reason="previous worker never acknowledged")

    if shuffle.is_cancelled(job_id):
        log.info("map_task.skipped_cancelled")
        return None

    t0 = time.perf_counter()
    try:
        if settings.map_task_sleep_ms:
            time.sleep(settings.map_task_sleep_ms / 1000)
        summary = map_tasks.process_partition(job_id, partition, trace_ids)
    except TRANSIENT as exc:
        if self.request.retries >= self.max_retries:
            shuffle.mark_failed(job_id, partition.key, f"{type(exc).__name__}: {exc}")
            log.error("map_task.failed_permanently", error=str(exc), retries=self.request.retries)
            return None
        log.warning("map_task.retry", error=type(exc).__name__, retry=self.request.retries + 1, countdown=_backoff(self.request.retries))
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries))
    except Exception as exc:  # a bug, not a blip: do not retry forever
        shuffle.mark_failed(job_id, partition.key, f"{type(exc).__name__}: {exc}")
        log.error("map_task.failed", exc_info=exc)
        return None

    log.info(
        "map_task.completed",
        rows_read=summary["rows_read"],
        keys_emitted=summary["keys_emitted"],
        duration_ms=round((time.perf_counter() - t0) * 1000, 1),
        redelivered=redelivered,
    )
    for cid in summary["traced"]:
        log.info("map_task.traced_event", correlation_id=cid, rows_read=summary["rows_read"], emitted_key=partition.key)
    # Deliberately tiny: the chord stores one result per map task in the backend.
    return summary["rows_read"]


# ---------------------------------------------------------------------- reduce
@celery_app.task(bind=True, name="pipeline.reduce_job", ignore_result=True, max_retries=None, acks_late=True)
def reduce_job(self, _map_results, job_id: str, correlation_id: str | None = None, db_attempt: int = 0):
    _bind(correlation_id, job_id=job_id, task="reduce")
    try:
        outcome = run_reduce(job_id)
    except BarrierNotReady as exc:
        # Not an error: maps still outstanding (e.g. one is being redelivered).
        log.info("reduce.waiting", pending=exc.pending)
        raise self.retry(countdown=2, kwargs={"db_attempt": db_attempt})
    except TRANSIENT as exc:
        if db_attempt >= settings.reduce_max_retries:
            log.error("reduce.failed_permanently", error=str(exc), attempts=db_attempt)
            mark_job_failed(job_id, {"code": "reduce_failed", "message": f"{type(exc).__name__}: {str(exc)[:250]}"})
            return
        log.warning("reduce.retry", error=type(exc).__name__, attempt=db_attempt + 1)
        raise self.retry(exc=exc, countdown=_backoff(db_attempt), kwargs={"db_attempt": db_attempt + 1})
    except DBAPIError as exc:
        log.error("reduce.database_error", exc_info=exc)
        mark_job_failed(job_id, {"code": "database_error", "message": str(exc.orig)[:300] if exc.orig else str(exc)[:300]})
        return
    except Exception as exc:
        log.error("reduce.failed", exc_info=exc)
        mark_job_failed(job_id, {"code": "reduce_failed", "message": f"{type(exc).__name__}: {str(exc)[:250]}"})
        return

    if outcome is not None:
        from app.services.cache import bump_analytics_version

        bump_analytics_version()
        log.info("job.finished", status=outcome["status"], events_processed=outcome["events_processed"])


# ---------------------------------------------------------------- bulk ingest
@celery_app.task(bind=True, name="ingest.load_batch", ignore_result=True, max_retries=3, acks_late=True)
def load_batch(self, batch_id: str, source_uri: str, correlation_id: str | None = None, key_id: int | None = None):
    _bind(correlation_id, batch_id=batch_id, task="load_batch")
    from app.services.ingest import run_batch_load

    try:
        run_batch_load(batch_id, source_uri, correlation_id, key_id)
    except TRANSIENT as exc:
        if self.request.retries >= self.max_retries:
            log.error("batch.failed_permanently", error=str(exc))
            from app.services.ingest import mark_batch_failed

            mark_batch_failed(batch_id, {"code": "transient_error", "message": str(exc)[:300]})
            return
        log.warning("batch.retry", error=type(exc).__name__)
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries))
