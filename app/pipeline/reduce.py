"""Stages 4 and 5: reduce and persist.

The Celery chord fires the reducer only after every map task has completed (the
barrier). The reducer folds the partial accumulators per key and computes what
cannot be computed partially:

* daily admission figures (counts, averages, peak hour, source mix)
* hourly occupancy, reconstructed as a running sum of per-hour bed deltas on top of
  the department's opening occupancy
* anomaly windows, via a rolling seasonal baseline (see anomaly.py)

Results are written with INSERT .. ON CONFLICT DO UPDATE inside one transaction, so
a job writes all of its rows or none, and re-running or re-delivering it overwrites
instead of duplicating. That idempotence is what makes the failure experiment safe.
"""
import csv
import io
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import numpy as np
import structlog
from psycopg2.extras import execute_values
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.core.redis import get_state_redis
from app.models.analytics import AnalysisJob
from app.pipeline import anomaly, shuffle
from app.pipeline.partition import (
    ADMISSION_JOBS,
    ANOMALY_JOBS,
    RESOURCE_JOBS,
    JobPlan,
    Partition,
    make_plan,
    plan_partitions,
)

log = structlog.get_logger("reduce")

# Occupancy is meaningless while the dataset itself is still filling up from empty (the first
# stays have no predecessors). Those days are excluded from the occupancy baseline entirely,
# not just from the flags, or the ramp would poison what "normal" means.
OCCUPANCY_WARMUP_DAYS = 21


class BarrierNotReady(Exception):
    def __init__(self, pending: int):
        super().__init__(f"{pending} partitions still outstanding")
        self.pending = pending


@dataclass(frozen=True)
class DeptInfo:
    department_id: int
    name: str
    dept_type: str
    bed_capacity: int
    staff_count: int


@dataclass
class ReduceResults:
    admission_rows: list[dict] = field(default_factory=list)
    resource_rows: list[dict] = field(default_factory=list)
    anomaly_rows: list[dict] = field(default_factory=list)
    events_processed: int = 0
    keys_folded: int = 0
    traced: list[tuple[str, str]] = field(default_factory=list)  # (correlation_id, partition key)


# --------------------------------------------------------------------- compute
def compute_results(
    *,
    job_type: str,
    plan: JobPlan,
    dept_ids: list[int],
    depts: dict[int, DeptInfo],
    accs: dict[str, dict],
    opening: dict[int, int],
    failed_keys: set[str],
) -> ReduceResults:
    """Pure function: accumulators in, result rows out. No I/O, so it is unit-testable."""
    out = ReduceResults()
    if plan.empty:
        return out

    want_adm = job_type in ADMISSION_JOBS
    want_res = job_type in RESOURCE_JOBS
    want_anom = job_type in ANOMALY_JOBS
    failed_depts = {int(k.split(":")[1]) for k in failed_keys}

    n_days = plan.days
    dates = [plan.range_start + timedelta(days=i) for i in range(n_days)]
    first_write = max((plan.start - plan.range_start).days, 0)  # index of first requested day
    occ_valid_from = 0
    if plan.data_start is not None:
        occ_valid_from = max((plan.data_start + timedelta(days=OCCUPANCY_WARMUP_DAYS) - plan.range_start).days, 0)

    for dept_id in dept_ids:
        info = depts[dept_id]
        dept_failed = dept_id in failed_depts
        adm_hourly = np.zeros(n_days * 24)
        occ = np.zeros(n_days * 24)
        daily_adm = np.zeros(n_days)
        running = int(opening.get(dept_id, 0))

        for i, d in enumerate(dates):
            key = f"{d.isoformat()}:{dept_id}"
            base = i * 24
            acc = accs.get(key)
            if acc is None:
                occ[base : base + 24] = running
                continue
            out.keys_folded += 1
            out.events_processed += acc["n"]
            out.traced.extend((cid, key) for cid in acc["traced"])
            adm_hourly[base : base + 24] = acc["hour_adm"]
            occ[base : base + 24] = running + np.cumsum(acc["delta"])
            running = int(occ[base + 23])
            daily_adm[i] = acc["adm"]

            if want_adm and i >= first_write:
                adm = acc["adm"]
                out.admission_rows.append(
                    {
                        "metric_date": d,
                        "department_id": dept_id,
                        "admission_count": adm,
                        "discharge_count": acc["dis"],
                        "transfer_count": acc["xfer"],
                        "avg_severity": round(acc["sev_sum"] / adm, 2) if adm else None,
                        "avg_length_of_stay_hours": round(acc["los_sum"] / acc["los_n"], 2) if acc["los_n"] else None,
                        "peak_hour": int(np.argmax(acc["hour_adm"])) if adm else None,
                        "source_breakdown": acc["src"],
                    }
                )

        if dept_failed:
            # A missing day breaks the running sum, so occupancy and anything derived
            # from it cannot be trusted for this department.
            continue

        cap = max(info.bed_capacity, 1)
        occupied = np.clip(occ, 0, None)
        if want_res:
            for i in range(first_write, n_days):
                d = dates[i]
                for h in range(24):
                    o = int(occupied[i * 24 + h])
                    rate = o / cap
                    out.resource_rows.append(
                        {
                            "metric_date": d,
                            "metric_hour": h,
                            "department_id": dept_id,
                            "occupied_beds": o,
                            "bed_capacity": info.bed_capacity,
                            "occupancy_rate": round(rate, 4),
                            "icu_occupancy_rate": round(rate, 4) if info.dept_type == "icu" else None,
                            "staff_to_patient_ratio": round(info.staff_count / max(o, 1), 3),
                        }
                    )

        if want_anom:
            found: list[dict] = []
            found += anomaly.detect(daily_adm, anomaly.DAILY_ADMISSIONS, plan.range_start, first_write)
            found += anomaly.detect(adm_hourly, anomaly.HOURLY_ADMISSIONS, plan.range_start, first_write * 24)
            if occ_valid_from < n_days:
                occ_series = occupied[occ_valid_from * 24 :] / cap
                occ_origin = plan.range_start + timedelta(days=occ_valid_from)
                found += anomaly.detect(
                    occ_series, anomaly.OCCUPANCY, occ_origin, max(first_write - occ_valid_from, 0) * 24
                )
            for w in found:
                w["department_id"] = dept_id
            out.anomaly_rows.extend(found)

    return out


# -------------------------------------------------------------------- database
def load_departments(conn, dept_ids: list[int]) -> dict[int, DeptInfo]:
    rows = conn.execute(
        text(
            "SELECT department_id, name, dept_type, bed_capacity, staff_count "
            "FROM raw.departments WHERE department_id = ANY(:ids)"
        ),
        {"ids": dept_ids},
    )
    return {r.department_id: DeptInfo(*r) for r in rows}


def opening_occupancy(conn, dept_ids: list[int], before: date) -> dict[int, int]:
    """Occupied beds per department at the start of the mapped range: the sum of all
    bed deltas strictly before it. This is the seed for the reducer's running sum."""
    t = datetime(before.year, before.month, before.day, tzinfo=UTC)
    rows = conn.execute(
        text(
            """
            SELECT department_id,
                   COALESCE(SUM(CASE WHEN event_type IN ('admission','icu_admit','transfer') THEN 1
                                     WHEN event_type IN ('discharge','icu_discharge') THEN -1
                                     ELSE 0 END), 0)::int AS occ
            FROM raw.hospital_events
            WHERE event_time < :t AND department_id = ANY(:ids)
            GROUP BY department_id
            """
        ),
        {"t": t, "ids": dept_ids},
    )
    return {r.department_id: int(r.occ) for r in rows}


_ADMISSION_COLS = [
    "metric_date", "department_id", "admission_count", "discharge_count", "transfer_count",
    "avg_severity", "avg_length_of_stay_hours", "peak_hour", "source_breakdown", "job_id",
]
_RESOURCE_COLS = [
    "metric_date", "metric_hour", "department_id", "occupied_beds", "bed_capacity", "occupancy_rate",
    "icu_occupancy_rate", "staff_to_patient_ratio", "job_id",
]

_ANOMALY_UPSERT = """
INSERT INTO analytics.anomaly_windows
  (window_start, window_end, department_id, metric_name, observed_value, expected_value,
   z_score, severity_label, job_id)
VALUES %s
ON CONFLICT (department_id, metric_name, window_start) DO UPDATE SET
  window_end = EXCLUDED.window_end, observed_value = EXCLUDED.observed_value,
  expected_value = EXCLUDED.expected_value, z_score = EXCLUDED.z_score,
  severity_label = EXCLUDED.severity_label, job_id = EXCLUDED.job_id
"""


def bulk_upsert(cur, table: str, columns: list[str], rows, conflict: list[str]) -> int:
    """COPY rows into a staging table, then upsert them in one in-server statement.

    Row-by-row (or even batched) INSERT .. ON CONFLICT over a client connection spends most
    of its time on round trips; COPY + INSERT..SELECT keeps the work inside Postgres and is
    several times faster, which matters because this is the serial tail of every job.
    Still one transaction, still idempotent."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    n = 0
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
        n += 1
    if n == 0:
        return 0
    buf.seek(0)
    cols = ", ".join(columns)
    cur.execute(f"CREATE TEMP TABLE _stage (LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP")
    cur.copy_expert(f"COPY _stage ({cols}) FROM STDIN WITH (FORMAT csv)", buf)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c not in conflict)
    cur.execute(
        f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _stage "
        f"ON CONFLICT ({', '.join(conflict)}) DO UPDATE SET {updates}"
    )
    cur.execute("DROP TABLE _stage")
    return n


def persist_results(
    *,
    job_id: str,
    job_type: str,
    plan: JobPlan,
    dept_ids: list[int],
    failed_depts: set[int],
    results: ReduceResults,
    status: str,
    error_detail: dict | None,
    partitions_completed: int,
) -> dict:
    """Write every result row and flip the job row in one transaction."""
    counts = {"admission_metrics": 0, "resource_metrics": 0, "anomaly_windows": 0}
    with engine.begin() as conn:
        cur = conn.connection.cursor()

        if results.admission_rows:
            counts["admission_metrics"] = bulk_upsert(
                cur,
                "analytics.admission_metrics",
                _ADMISSION_COLS,
                (
                    (
                        r["metric_date"], r["department_id"], r["admission_count"], r["discharge_count"],
                        r["transfer_count"], r["avg_severity"], r["avg_length_of_stay_hours"], r["peak_hour"],
                        json.dumps(r["source_breakdown"]), job_id,
                    )
                    for r in results.admission_rows
                ),
                ["metric_date", "department_id"],
            )

        if results.resource_rows:
            counts["resource_metrics"] = bulk_upsert(
                cur,
                "analytics.resource_metrics",
                _RESOURCE_COLS,
                (
                    (
                        r["metric_date"], r["metric_hour"], r["department_id"], r["occupied_beds"], r["bed_capacity"],
                        r["occupancy_rate"], r["icu_occupancy_rate"], r["staff_to_patient_ratio"], job_id,
                    )
                    for r in results.resource_rows
                ),
                ["metric_date", "metric_hour", "department_id"],
            )

        if job_type in ANOMALY_JOBS and not plan.empty:
            ok_depts = [d for d in dept_ids if d not in failed_depts]
            # The requested window is authoritative: clear windows from earlier runs that
            # this run no longer reports, then upsert the fresh ones.
            window_end = datetime(plan.end.year, plan.end.month, plan.end.day, tzinfo=UTC) + timedelta(days=1)
            window_start = datetime(plan.start.year, plan.start.month, plan.start.day, tzinfo=UTC)
            cur.execute(
                "DELETE FROM analytics.anomaly_windows "
                "WHERE department_id = ANY(%s) AND window_start >= %s AND window_start < %s",
                (ok_depts, window_start, window_end),
            )
            if results.anomaly_rows:
                execute_values(
                    cur,
                    _ANOMALY_UPSERT,
                    [
                        (
                            r["window_start"], r["window_end"], r["department_id"], r["metric_name"],
                            round(r["observed_value"], 4), round(r["expected_value"], 4),
                            round(r["z_score"], 3), r["severity_label"], job_id,
                        )
                        for r in results.anomaly_rows
                    ],
                    page_size=2000,
                )
                counts["anomaly_windows"] = len(results.anomaly_rows)

        conn.execute(
            text(
                """
                UPDATE analytics.analysis_jobs
                SET status = :status,
                    finished_at = clock_timestamp(),
                    duration_seconds = round(EXTRACT(EPOCH FROM (clock_timestamp() - COALESCE(started_at, created_at)))::numeric, 3),
                    events_processed = :events,
                    partitions_completed = :done,
                    error_detail = CAST(:err AS jsonb)
                WHERE job_id = :id
                """
            ),
            {
                "status": status,
                "events": results.events_processed,
                "done": partitions_completed,
                "err": json.dumps(error_detail) if error_detail else None,
                "id": job_id,
            },
        )
    return counts


def mark_job_failed(job_id: str, error_detail: dict) -> None:
    """Record a terminal failure on the job row, and in Redis if the database is down."""
    shuffle.set_failure_note(job_id, error_detail)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE analytics.analysis_jobs
                    SET status = 'failed', finished_at = clock_timestamp(),
                        duration_seconds = round(EXTRACT(EPOCH FROM (clock_timestamp() - COALESCE(started_at, created_at)))::numeric, 3),
                        error_detail = CAST(:err AS jsonb)
                    WHERE job_id = :id AND status IN ('queued', 'running')
                    """
                ),
                {"err": json.dumps(error_detail), "id": job_id},
            )
    except Exception as exc:  # the database is the thing that failed; the Redis note is the record
        log.error("job.mark_failed.db_unavailable", job_id=job_id, error=str(exc))


# ----------------------------------------------------------------- orchestration
def job_plan_from_params(job_type: str, params: dict) -> tuple[JobPlan, list[int]]:
    start = date.fromisoformat(params["start_date"])
    end = date.fromisoformat(params["end_date"])
    ds = date.fromisoformat(params["data_start"]) if params.get("data_start") else None
    de = date.fromisoformat(params["data_end"]) if params.get("data_end") else None
    return make_plan(job_type, start, end, ds, de), [int(d) for d in params["departments"]]


def run_reduce(job_id: str) -> dict | None:
    """Everything the chord callback does. Raises BarrierNotReady while maps are outstanding."""
    t_start = time.perf_counter()
    with SessionLocal() as session:
        job = session.get(AnalysisJob, uuid.UUID(job_id))
        if job is None:
            log.error("reduce.job_not_found", job_id=job_id)
            return None
        status, job_type, params = job.status, job.job_type, dict(job.params)

    if status in ("completed", "cancelled", "failed"):
        log.info("reduce.skipped_already_final", job_id=job_id, status=status)
        return None
    if shuffle.is_cancelled(job_id):
        log.info("reduce.skipped_cancelled", job_id=job_id)
        return None

    plan, dept_ids = job_plan_from_params(job_type, params)
    partitions: list[Partition] = plan_partitions(plan, dept_ids)
    expected = [p.key for p in partitions]
    expected_set = set(expected)

    # Barrier safety net. The chord already guarantees this, but redelivery and a
    # restarted broker can fire it early, so the reducer verifies for itself.
    done = shuffle.done_keys(job_id) & expected_set
    failed = {k: v for k, v in shuffle.failed_partitions(job_id).items() if k in expected_set}
    pending = expected_set - done - set(failed)
    if pending:
        r = get_state_redis()
        r.set(f"job:{job_id}:reduce_started", str(time.time()), nx=True, ex=settings.shuffle_ttl_seconds)
        waited = time.time() - float(r.get(f"job:{job_id}:reduce_started") or time.time())
        if waited < settings.reduce_barrier_max_wait_seconds:
            raise BarrierNotReady(len(pending))
        log.error("reduce.barrier_timeout", pending=len(pending), waited_s=round(waited, 1))
        for k in pending:
            failed[k] = "partition never completed (barrier timeout)"

    t_read = time.perf_counter()
    accs_raw = shuffle.read_accumulators(job_id, sorted(done))
    accs: dict[str, dict] = {}
    for k, v in accs_raw.items():
        if v is None:
            failed[k] = "shuffle data missing (Redis restarted or key expired)"
        else:
            accs[k] = v
    log.info(
        "reduce.started",
        job_id=job_id,
        job_type=job_type,
        partitions=len(expected),
        keys_available=len(accs),
        keys_failed=len(failed),
    )

    with engine.connect() as conn:
        depts = load_departments(conn, dept_ids)
        opening = (
            opening_occupancy(conn, dept_ids, plan.range_start)
            if (job_type in RESOURCE_JOBS and not plan.empty)
            else {}
        )
    missing_depts = [d for d in dept_ids if d not in depts]
    if missing_depts:
        raise ValueError(f"unknown departments {missing_depts}")

    t_compute = time.perf_counter()
    results = compute_results(
        job_type=job_type,
        plan=plan,
        dept_ids=dept_ids,
        depts=depts,
        accs=accs,
        opening=opening,
        failed_keys=set(failed),
    )
    t_persist = time.perf_counter()
    log.info(
        "reduce.folded",
        job_id=job_id,
        keys_folded=results.keys_folded,
        events=results.events_processed,
        load_ms=round((t_compute - t_read) * 1000),
        compute_ms=round((t_persist - t_compute) * 1000),
    )
    for cid, key in results.traced:
        # One line per followed event: the end of its trail through the pipeline.
        log.info("reduce.traced_event", correlation_id=cid, job_id=job_id, partition=key, folded_into=key)

    if failed and not accs:
        status_out = "failed"
    elif failed:
        status_out = "partial"
    else:
        status_out = "completed"
    error_detail = None
    if failed:
        error_detail = {
            "code": "partitions_failed",
            "failed_partitions": len(failed),
            "total_partitions": len(expected),
            "sample": [{"partition": k, "error": v} for k, v in sorted(failed.items())[:20]],
        }
    failed_depts = {int(k.split(":")[1]) for k in failed}

    counts = persist_results(
        job_id=job_id,
        job_type=job_type,
        plan=plan,
        dept_ids=dept_ids,
        failed_depts=failed_depts,
        results=results,
        status=status_out,
        error_detail=error_detail,
        partitions_completed=len(accs),
    )
    log.info(
        "reduce.rows_written",
        job_id=job_id,
        status=status_out,
        duration_ms=round((time.perf_counter() - t_start) * 1000),
        persist_ms=round((time.perf_counter() - t_persist) * 1000),
        **counts,
    )
    for cid, key in results.traced:
        log.info("reduce.traced_event_persisted", correlation_id=cid, job_id=job_id, partition=key, **counts)
    return {"status": status_out, **counts, "events_processed": results.events_processed}
