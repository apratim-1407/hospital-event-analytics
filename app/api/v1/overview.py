"""Dashboard-friendly summaries for the web UI (any role, read-only).

The analytics endpoints return one row per (day, department) or (day, hour, department).
A person looking at a screen wants the answer already added up: how many admissions, who
is busiest, when are beds fullest. These endpoints do that on the server, from the same
pre-computed analytics.* tables (never the raw events), and cache the result like the rest.
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.v1.analytics import _serve
from app.core.db import get_db
from app.core.deps import Principal, require_role
from app.core.errors import APIError
from app.core.security import Role

router = APIRouter(prefix="/overview", tags=["overview"])


# ---------------------------------------------------------------- schemas
class DepartmentInfo(BaseModel):
    department_id: int
    name: str
    dept_type: str
    bed_capacity: int
    staff_count: int


class DateSpan(BaseModel):
    first: date
    last: date


class DepartmentList(BaseModel):
    departments: list[DepartmentInfo]
    analysed_range: DateSpan | None


class BusyDay(BaseModel):
    date: date
    admissions: int


class DepartmentSummary(BaseModel):
    department_id: int
    name: str
    dept_type: str
    bed_capacity: int
    admissions: int
    discharges: int
    avg_stay_hours: float | None
    avg_severity: float | None
    busiest_hour: int | None
    busiest_day: BusyDay | None
    bed_use_avg: float | None
    bed_use_peak: float | None
    hours_over_90: int


class Totals(BaseModel):
    admissions: int
    discharges: int
    avg_stay_hours: float | None
    avg_severity: float | None


class TrendPoint(BaseModel):
    period: date
    admissions: int


class SummaryOut(BaseModel):
    start: date
    end: date
    days: int
    granularity: str
    totals: Totals
    busiest_day: BusyDay | None
    busiest_department: str | None
    fullest_department: str | None
    departments: list[DepartmentSummary]
    trend: list[TrendPoint]


class WeeklyPattern(BaseModel):
    weekday: list[float | None]
    weekend: list[float | None]


# ---------------------------------------------------------------- endpoints
@router.get(
    "/departments",
    response_model=DepartmentList,
    summary="Department names and sizes, plus the dates that have results (any role)",
)
def departments(_: Principal = Depends(require_role(Role.VIEWER)), db: Session = Depends(get_db)):
    def compute():
        depts = db.execute(
            text("SELECT department_id, name, dept_type, bed_capacity, staff_count FROM raw.departments ORDER BY department_id")
        ).mappings().all()
        span = db.execute(
            text("SELECT min(metric_date) AS first, max(metric_date) AS last FROM analytics.admission_metrics")
        ).mappings().one()
        db.close()
        return DepartmentList(
            departments=[DepartmentInfo(**d) for d in depts],
            analysed_range=DateSpan(first=span["first"], last=span["last"]) if span["first"] else None,
        ).model_dump_json()

    return _serve("overview-departments", {}, compute)


def _granularity(days: int) -> str:
    return "day" if days <= 92 else ("week" if days <= 800 else "month")


def _next_period(d: date, g: str) -> date:
    if g == "week":
        return d + timedelta(days=7)
    if g == "month":
        return date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return d + timedelta(days=1)


@router.get(
    "/summary",
    response_model=SummaryOut,
    summary="Totals, busiest department, bed use and an admissions trend for a period (any role)",
)
def summary(
    _: Principal = Depends(require_role(Role.VIEWER)),
    db: Session = Depends(get_db),
    start_date: date = Query(...),
    end_date: date = Query(...),
    department_id: int | None = Query(None, ge=1),
):
    if end_date < start_date:
        raise APIError(400, "validation_error", "end_date must not be before start_date")

    def compute():
        params = {"sd": start_date, "ed": end_date}
        cond = ""
        if department_id:
            cond, params["dept"] = " AND department_id = :dept", department_id

        per_dept = db.execute(
            text(
                f"""
                SELECT a.department_id, d.name, d.dept_type, d.bed_capacity,
                       sum(a.admission_count)::int AS admissions,
                       sum(a.discharge_count)::int AS discharges,
                       (sum(a.avg_severity * a.admission_count)
                          / NULLIF(sum(a.admission_count) FILTER (WHERE a.avg_severity IS NOT NULL), 0))::float8 AS avg_severity,
                       (sum(a.avg_length_of_stay_hours * a.discharge_count) FILTER (WHERE a.avg_length_of_stay_hours IS NOT NULL)
                          / NULLIF(sum(a.discharge_count) FILTER (WHERE a.avg_length_of_stay_hours IS NOT NULL), 0))::float8 AS avg_stay_hours,
                       mode() WITHIN GROUP (ORDER BY a.peak_hour) AS busiest_hour
                FROM analytics.admission_metrics a JOIN raw.departments d USING (department_id)
                WHERE a.metric_date BETWEEN :sd AND :ed {cond.replace('department_id', 'a.department_id')}
                GROUP BY a.department_id, d.name, d.dept_type, d.bed_capacity
                ORDER BY a.department_id
                """
            ),
            params,
        ).mappings().all()

        peak_days = {
            r["department_id"]: r
            for r in db.execute(
                text(
                    f"""
                    SELECT DISTINCT ON (department_id) department_id, metric_date AS date, admission_count AS admissions
                    FROM analytics.admission_metrics
                    WHERE metric_date BETWEEN :sd AND :ed {cond}
                    ORDER BY department_id, admission_count DESC, metric_date
                    """
                ),
                params,
            ).mappings()
        }
        beds = {
            r["department_id"]: r
            for r in db.execute(
                text(
                    f"""
                    SELECT department_id,
                           avg(occupancy_rate)::float8 AS avg_use,
                           max(occupancy_rate)::float8 AS peak_use,
                           count(*) FILTER (WHERE occupancy_rate >= 0.9)::int AS hours_over_90
                    FROM analytics.resource_metrics
                    WHERE metric_date BETWEEN :sd AND :ed {cond}
                    GROUP BY department_id
                    """
                ),
                params,
            ).mappings()
        }

        days = (end_date - start_date).days + 1
        g = _granularity(days)
        trend = [
            (r.period, r.admissions)
            for r in db.execute(
                text(
                    f"""
                    SELECT date_trunc('{g}', metric_date)::date AS period, sum(admission_count)::int AS admissions
                    FROM analytics.admission_metrics
                    WHERE metric_date BETWEEN :sd AND :ed {cond}
                    GROUP BY 1 ORDER BY 1
                    """
                ),
                params,
            ).all()
        ]
        busiest = db.execute(
            text(
                f"""
                SELECT metric_date AS date, sum(admission_count)::int AS admissions
                FROM analytics.admission_metrics
                WHERE metric_date BETWEEN :sd AND :ed {cond}
                GROUP BY metric_date ORDER BY 2 DESC, 1 LIMIT 1
                """
            ),
            params,
        ).mappings().first()
        db.close()  # free the pooled connection before building the response

        # Weekly/monthly buckets cut off by the period's edges would show as false dips; drop them.
        if g != "day" and trend:
            if trend[0][0] < start_date:
                trend = trend[1:]
            if trend and _next_period(trend[-1][0], g) > end_date + timedelta(days=1):
                trend = trend[:-1]

        departments_out, tot_adm, tot_dis, stay_num, stay_den, sev_num, sev_den = [], 0, 0, 0.0, 0, 0.0, 0
        for r in per_dept:
            b = beds.get(r["department_id"])
            pk = peak_days.get(r["department_id"])
            departments_out.append(
                DepartmentSummary(
                    **{k: r[k] for k in ("department_id", "name", "dept_type", "bed_capacity", "admissions", "discharges", "avg_stay_hours", "avg_severity", "busiest_hour")},
                    busiest_day=BusyDay(date=pk["date"], admissions=pk["admissions"]) if pk else None,
                    bed_use_avg=b["avg_use"] if b else None,
                    bed_use_peak=b["peak_use"] if b else None,
                    hours_over_90=b["hours_over_90"] if b else 0,
                )
            )
            tot_adm += r["admissions"]
            tot_dis += r["discharges"]
            if r["avg_stay_hours"] is not None:
                stay_num += r["avg_stay_hours"] * r["discharges"]
                stay_den += r["discharges"]
            if r["avg_severity"] is not None:
                sev_num += r["avg_severity"] * r["admissions"]
                sev_den += r["admissions"]

        with_beds = [d for d in departments_out if d.bed_use_avg is not None]
        return SummaryOut(
            start=start_date, end=end_date, days=days, granularity=g,
            totals=Totals(
                admissions=tot_adm, discharges=tot_dis,
                avg_stay_hours=stay_num / stay_den if stay_den else None,
                avg_severity=sev_num / sev_den if sev_den else None,
            ),
            busiest_day=BusyDay(**busiest) if busiest else None,
            busiest_department=max(departments_out, key=lambda d: d.admissions).name if departments_out else None,
            fullest_department=max(with_beds, key=lambda d: d.bed_use_avg).name if with_beds else None,
            departments=departments_out,
            trend=[TrendPoint(period=p, admissions=n) for p, n in trend],
        ).model_dump_json()

    return _serve("overview-summary", dict(s=start_date, e=end_date, d=department_id), compute)


@router.get(
    "/weekly-pattern",
    response_model=WeeklyPattern,
    summary="Average share of beds in use by hour of day, weekdays vs weekends (any role)",
)
def weekly_pattern(
    _: Principal = Depends(require_role(Role.VIEWER)),
    db: Session = Depends(get_db),
    start_date: date = Query(...),
    end_date: date = Query(...),
    department_id: int | None = Query(None, ge=1),
):
    def compute():
        params = {"sd": start_date, "ed": end_date}
        cond = ""
        if department_id:
            cond, params["dept"] = " AND department_id = :dept", department_id
        rows = db.execute(
            text(
                f"""
                SELECT (EXTRACT(isodow FROM metric_date) >= 6) AS weekend, metric_hour,
                       (sum(occupied_beds)::float8 / NULLIF(sum(bed_capacity), 0)) AS use
                FROM analytics.resource_metrics
                WHERE metric_date BETWEEN :sd AND :ed {cond}
                GROUP BY 1, 2
                """
            ),
            params,
        ).all()
        db.close()
        out = {"weekday": [None] * 24, "weekend": [None] * 24}
        for weekend, hour, use in rows:
            out["weekend" if weekend else "weekday"][int(hour)] = None if use is None else round(use, 4)
        return WeeklyPattern(**out).model_dump_json()

    return _serve("overview-pattern", dict(s=start_date, e=end_date, d=department_id), compute)
