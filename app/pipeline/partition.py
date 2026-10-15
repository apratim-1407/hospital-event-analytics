"""Stage 1 of the pipeline: split a job into independent partitions.

One partition = one (day x department) slice of raw events. That grain produces
slices of roughly equal size, which avoids the classic MapReduce failure mode of
one oversized partition holding up the whole job (partitioning by patient, for
example, would be wildly uneven).

The API and the worker both import this module, so the partition count promised in
the 202 response is exactly the number of tasks the coordinator dispatches.
"""
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

# Detection needs history to learn what "normal" is. Jobs that run anomaly
# detection read this many extra days before the requested window (results are
# still only written for the requested window).
LOOKBACK_DAYS = 35

ADMISSION_JOBS = {"admission_patterns", "full_analysis"}
RESOURCE_JOBS = {"resource_utilisation", "anomaly_detection", "full_analysis"}
ANOMALY_JOBS = {"anomaly_detection", "full_analysis"}


@dataclass(frozen=True)
class Partition:
    day: date
    department_id: int

    @property
    def key(self) -> str:
        return f"{self.day.isoformat()}:{self.department_id}"

    @property
    def bounds(self) -> tuple[datetime, datetime]:
        t0 = datetime(self.day.year, self.day.month, self.day.day, tzinfo=UTC)
        return t0, t0 + timedelta(days=1)


@dataclass(frozen=True)
class JobPlan:
    """Resolved date range for a job (after clamping to where data actually exists)."""

    start: date            # requested window (what gets written)
    end: date
    range_start: date      # first day mapped (includes look-back for anomaly jobs)
    range_end: date        # last day mapped
    data_start: date | None
    data_end: date | None
    empty: bool

    @property
    def days(self) -> int:
        return 0 if self.empty else (self.range_end - self.range_start).days + 1


def fetch_data_bounds(conn: Connection) -> tuple[date | None, date | None]:
    """First and last event day (UTC). Cheap: served from the per-partition time indexes."""
    row = conn.execute(
        text(
            "SELECT (min(event_time) AT TIME ZONE 'UTC')::date, (max(event_time) AT TIME ZONE 'UTC')::date "
            "FROM raw.hospital_events"
        )
    ).one()
    return row[0], row[1]


def make_plan(job_type: str, start: date, end: date, data_start: date | None, data_end: date | None) -> JobPlan:
    if data_start is None or data_end is None:
        return JobPlan(start, end, start, end, None, None, empty=True)
    range_start = start - timedelta(days=LOOKBACK_DAYS) if job_type in ANOMALY_JOBS else start
    range_start = max(range_start, data_start)
    range_end = min(end, data_end)
    empty = range_start > range_end
    return JobPlan(start, end, range_start, range_end, data_start, data_end, empty)


def plan_partitions(plan: JobPlan, department_ids: list[int]) -> list[Partition]:
    if plan.empty:
        return []
    out: list[Partition] = []
    day = plan.range_start
    depts = sorted(department_ids)
    while day <= plan.range_end:
        out.extend(Partition(day, d) for d in depts)
        day += timedelta(days=1)
    return out


def parse_partition_key(key: str) -> Partition:
    day, dept = key.split(":")
    return Partition(date.fromisoformat(day), int(dept))
