"""Query plane: serves pre-computed results in milliseconds.

These endpoints read only analytics.* (small, indexed, derived) and never the raw
table, so latency depends on the requested date range and department count rather than
on how many million raw events sit underneath. Responses are cached for 60 seconds.
"""
import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import Principal, require_role
from app.core.errors import APIError
from app.core.security import Role
from app.schemas.analytics import (
    AdmissionPage,
    AdmissionRow,
    AnomalyPage,
    AnomalyRow,
    ResourcePage,
    ResourceRow,
)
from app.schemas.common import decode_cursor, encode_cursor
from app.services import cache

router = APIRouter(prefix="/analytics", tags=["analytics"])

SEVERITIES = ("moderate", "high", "critical")
METRICS = ("daily_admissions", "hourly_admissions", "occupancy_rate")


def _dept_clause(depts: list[int] | None, params: dict, col: str = "department_id") -> str:
    if not depts:
        return ""
    params["depts"] = depts
    return f" AND {col} = ANY(:depts)"


def _serve(endpoint: str, cache_params: dict, compute) -> Response:
    """Cache-aside. The body is stored and returned as an already-serialised JSON string:
    a hit costs one Redis GET and no parsing, validation or re-serialisation."""
    key = cache.make_key(endpoint, cache_params)
    cached = cache.get_cached(key)
    if cached is not None:
        return Response(cached, media_type="application/json", headers={"X-Cache": "HIT"})
    body = compute()
    cache.set_cached(key, body)
    return Response(body, media_type="application/json", headers={"X-Cache": "MISS"})


@router.get(
    "/admissions",
    response_model=AdmissionPage,
    summary="Admission patterns by date range and department (any role)",
)
def admissions(
    _: Principal = Depends(require_role(Role.VIEWER)),
    db: Session = Depends(get_db),
    start_date: date | None = None,
    end_date: date | None = None,
    department_id: list[int] | None = Query(None),
    job_id: uuid.UUID | None = Query(None, description="Only rows produced by this job"),
    limit: int = Query(100, ge=1, le=1000),
    cursor: str | None = None,
):
    cache_params = dict(s=start_date, e=end_date, d=sorted(department_id or []), j=job_id, l=limit, c=cursor)

    def compute():
        params: dict = {"lim": limit + 1}
        sql = (
            "SELECT metric_date, department_id, admission_count, discharge_count, transfer_count, "
            "avg_severity::float8 AS avg_severity, avg_length_of_stay_hours::float8 AS avg_length_of_stay_hours, "
            "peak_hour, source_breakdown, job_id FROM analytics.admission_metrics WHERE TRUE"
        )
        if start_date:
            sql += " AND metric_date >= :sd"
            params["sd"] = start_date
        if end_date:
            sql += " AND metric_date <= :ed"
            params["ed"] = end_date
        if job_id:
            sql += " AND job_id = :jid"
            params["jid"] = job_id
        sql += _dept_clause(department_id, params)
        c = decode_cursor(cursor)
        if c:
            sql += " AND (metric_date, department_id) > (CAST(:cd AS date), :cdept)"
            params["cd"], params["cdept"] = c["d"], c["p"]
        sql += " ORDER BY metric_date, department_id LIMIT :lim"
        rows = db.execute(text(sql), params).mappings().all()
        db.close()  # free the pooled connection now, not at request teardown
        nxt = None
        if len(rows) > limit:
            rows = rows[:limit]
            nxt = encode_cursor({"d": rows[-1]["metric_date"].isoformat(), "p": rows[-1]["department_id"]})
        return AdmissionPage(data=[AdmissionRow(**r) for r in rows], next_cursor=nxt).model_dump_json()

    return _serve("admissions", cache_params, compute)


@router.get(
    "/resources",
    response_model=ResourcePage,
    summary="Occupancy and staffing utilisation at hourly grain (any role)",
)
def resources(
    _: Principal = Depends(require_role(Role.VIEWER)),
    db: Session = Depends(get_db),
    start_date: date | None = None,
    end_date: date | None = None,
    department_id: list[int] | None = Query(None),
    job_id: uuid.UUID | None = None,
    limit: int = Query(200, ge=1, le=2000),
    cursor: str | None = None,
):
    cache_params = dict(s=start_date, e=end_date, d=sorted(department_id or []), j=job_id, l=limit, c=cursor)

    def compute():
        params: dict = {"lim": limit + 1}
        sql = (
            "SELECT metric_date, metric_hour, department_id, occupied_beds, bed_capacity, "
            "occupancy_rate::float8 AS occupancy_rate, icu_occupancy_rate::float8 AS icu_occupancy_rate, "
            "staff_to_patient_ratio::float8 AS staff_to_patient_ratio, job_id "
            "FROM analytics.resource_metrics WHERE TRUE"
        )
        if start_date:
            sql += " AND metric_date >= :sd"
            params["sd"] = start_date
        if end_date:
            sql += " AND metric_date <= :ed"
            params["ed"] = end_date
        if job_id:
            sql += " AND job_id = :jid"
            params["jid"] = job_id
        sql += _dept_clause(department_id, params)
        c = decode_cursor(cursor)
        if c:
            sql += " AND (metric_date, metric_hour, department_id) > (CAST(:cd AS date), :ch, :cdept)"
            params["cd"], params["ch"], params["cdept"] = c["d"], c["h"], c["p"]
        sql += " ORDER BY metric_date, metric_hour, department_id LIMIT :lim"
        rows = db.execute(text(sql), params).mappings().all()
        db.close()  # free the pooled connection now, not at request teardown
        nxt = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            nxt = encode_cursor({"d": last["metric_date"].isoformat(), "h": last["metric_hour"], "p": last["department_id"]})
        return ResourcePage(data=[ResourceRow(**r) for r in rows], next_cursor=nxt).model_dump_json()

    return _serve("resources", cache_params, compute)


@router.get(
    "/anomalies",
    response_model=AnomalyPage,
    summary="Unusual workload windows, high or low, filterable by severity (any role)",
)
def anomalies(
    _: Principal = Depends(require_role(Role.VIEWER)),
    db: Session = Depends(get_db),
    start: datetime | None = Query(None, description="Windows ending after this instant"),
    end: datetime | None = Query(None, description="Windows starting before this instant"),
    department_id: list[int] | None = Query(None),
    severity: list[str] | None = Query(None, description="moderate | high | critical (repeatable)"),
    metric: str | None = Query(None, description="daily_admissions | hourly_admissions | occupancy_rate"),
    direction: str | None = Query(None, description="high | low"),
    job_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=1000),
    cursor: str | None = None,
):
    if severity and any(s not in SEVERITIES for s in severity):
        raise APIError(400, "validation_error", f"severity must be one of {SEVERITIES}")
    if metric and metric not in METRICS:
        raise APIError(400, "validation_error", f"metric must be one of {METRICS}")
    if direction and direction not in ("high", "low"):
        raise APIError(400, "validation_error", "direction must be 'high' or 'low'")

    cache_params = dict(
        s=start, e=end, d=sorted(department_id or []), sev=sorted(severity or []), m=metric, dr=direction,
        j=job_id, l=limit, c=cursor,
    )

    def compute():
        params: dict = {"lim": limit + 1}
        sql = (
            "SELECT anomaly_id, window_start, window_end, department_id, metric_name, "
            "observed_value::float8 AS observed_value, expected_value::float8 AS expected_value, "
            "z_score::float8 AS z_score, severity_label, "
            "CASE WHEN z_score >= 0 THEN 'high' ELSE 'low' END AS direction, job_id "
            "FROM analytics.anomaly_windows WHERE TRUE"
        )
        if start:
            sql += " AND window_end > :st"
            params["st"] = start
        if end:
            sql += " AND window_start < :en"
            params["en"] = end
        if severity:
            sql += " AND severity_label = ANY(:sev)"
            params["sev"] = severity
        if metric:
            sql += " AND metric_name = :metric"
            params["metric"] = metric
        if direction:
            sql += " AND z_score >= 0" if direction == "high" else " AND z_score < 0"
        if job_id:
            sql += " AND job_id = :jid"
            params["jid"] = job_id
        sql += _dept_clause(department_id, params)
        c = decode_cursor(cursor)
        if c:
            sql += " AND (window_start, anomaly_id) > (CAST(:cs AS timestamptz), CAST(:ci AS bigint))"
            params["cs"], params["ci"] = c["s"], c["i"]
        sql += " ORDER BY window_start, anomaly_id LIMIT :lim"
        rows = db.execute(text(sql), params).mappings().all()
        db.close()  # free the pooled connection now, not at request teardown
        nxt = None
        if len(rows) > limit:
            rows = rows[:limit]
            nxt = encode_cursor({"s": rows[-1]["window_start"].isoformat(), "i": rows[-1]["anomaly_id"]})
        return AnomalyPage(data=[AnomalyRow(**r) for r in rows], next_cursor=nxt).model_dump_json()

    return _serve("anomalies", cache_params, compute)
