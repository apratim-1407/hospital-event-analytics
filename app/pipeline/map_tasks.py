"""Stage 2: map.

One task handles exactly one partition and never looks outside it. It streams its
slice through a server-side cursor (memory stays flat regardless of slice size),
folds rows into a partial accumulator, and emits it to the shuffle.

Map tasks are pure and stateless: they read immutable raw data and write nothing to
the result tables. That purity is what makes them safely retryable, which is what
makes the worker-kill experiment work.
"""
from datetime import date

from sqlalchemy import text

from app.core.db import engine
from app.models.raw import (
    ARRIVAL_ADMISSION_TYPES,
    ARRIVAL_TRANSFER_TYPES,
    DEPARTURE_TYPES,
)
from app.pipeline import shuffle
from app.pipeline.partition import Partition

_EVENT_SQL = text(
    """
    SELECT event_type::text           AS event_type,
           severity                   AS severity,
           admission_source::text     AS source,
           EXTRACT(HOUR FROM event_time AT TIME ZONE 'UTC')::int AS hr,
           length_of_stay_hours       AS los,
           correlation_id::text       AS cid
    FROM raw.hospital_events
    WHERE event_time >= :t0 AND event_time < :t1 AND department_id = :dept
    """
)

_ADMISSION = set(ARRIVAL_ADMISSION_TYPES)
_TRANSFER = set(ARRIVAL_TRANSFER_TYPES)
_DEPARTURE = set(DEPARTURE_TYPES)


def new_accumulator() -> dict:
    return {
        "n": 0,            # raw rows read
        "adm": 0,          # admissions (admission + icu_admit)
        "dis": 0,          # discharges (discharge + icu_discharge)
        "xfer": 0,         # transfers into the department
        "surg": 0,         # surgical procedures
        "sev_sum": 0,      # severity summed over admissions
        "los_sum": 0.0,    # length of stay summed over discharges
        "los_n": 0,
        "hour_adm": [0] * 24,   # admissions per hour of day
        "src": {},              # admissions by admission_source
        "delta": [0] * 24,      # net change in occupied beds per hour
        "traced": [],           # correlation IDs of events being followed end to end
    }


def fold_row(acc: dict, event_type: str, severity: int, source: str, hour: int, los, cid: str | None, trace: set[str]) -> None:
    acc["n"] += 1
    if event_type in _ADMISSION:
        acc["adm"] += 1
        acc["sev_sum"] += int(severity)
        acc["hour_adm"][hour] += 1
        acc["src"][source] = acc["src"].get(source, 0) + 1
        acc["delta"][hour] += 1
    elif event_type in _TRANSFER:
        acc["xfer"] += 1
        acc["delta"][hour] += 1
    elif event_type in _DEPARTURE:
        acc["dis"] += 1
        acc["delta"][hour] -= 1
        if los is not None:
            acc["los_sum"] += float(los)
            acc["los_n"] += 1
    else:  # surgery: a procedure on an already-admitted patient, no bed change
        acc["surg"] += 1
    if trace and cid in trace:
        acc["traced"].append(cid)


def process_partition(job_id: str, partition: Partition, trace_ids: list[str] | None = None) -> dict:
    """Read one partition, fold it, write it to the shuffle. Returns a small summary."""
    t0, t1 = partition.bounds
    trace = set(trace_ids or [])
    acc = new_accumulator()
    with engine.connect() as conn:
        result = conn.execution_options(stream_results=True, max_row_buffer=2000).execute(
            _EVENT_SQL, {"t0": t0, "t1": t1, "dept": partition.department_id}
        )
        for row in result:
            fold_row(acc, row.event_type, row.severity, row.source, row.hr, row.los, row.cid, trace)
    shuffle.write_accumulator(job_id, partition.key, acc)
    return {"rows_read": acc["n"], "traced": list(acc["traced"]), "keys_emitted": 1}


def partition_from_args(day_iso: str, department_id: int) -> Partition:
    return Partition(date.fromisoformat(day_iso), int(department_id))
