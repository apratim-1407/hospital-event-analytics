"""The map and reduce logic, exercised without a database or broker: generated events are
folded into accumulators exactly as map tasks do, then reduced by the same pure function the
reducer uses, and checked against straight-line aggregation of the raw events."""
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
import pytest

from app.pipeline.map_tasks import fold_row, new_accumulator
from app.pipeline.partition import make_plan
from app.pipeline.reduce import DeptInfo, compute_results
from scripts.generate import EVENT_TYPE_NAMES, SOURCE_NAMES, generate

START, END = date(2025, 1, 1), date(2025, 3, 1)
EPOCH_ORDINAL = 719163  # date(1970, 1, 1).toordinal()


def _accumulate(gen, trace=frozenset()):
    ev = gen.events
    days = (ev["time"] // 86400).astype(int)
    hours = ((ev["time"] % 86400) // 3600).astype(int)
    accs = {}
    for dept in range(1, 9):
        for i in range((END - START).days + 1):
            accs[f"{(START + timedelta(days=i)).isoformat()}:{dept}"] = new_accumulator()  # empty partitions exist too
    for i in range(len(days)):
        d = date.fromordinal(EPOCH_ORDINAL + int(days[i]))
        key = f"{d.isoformat()}:{int(ev['dept'][i])}"
        los = ev["los"][i]
        fold_row(
            accs[key], EVENT_TYPE_NAMES[int(ev["type"][i])], int(ev["sev"][i]), SOURCE_NAMES[int(ev["src"][i])],
            int(hours[i]), None if np.isnan(los) else float(los), None, set(trace),
        )
    return accs


@pytest.fixture(scope="module")
def gen():
    return generate(30000, START, END, seed=11)


@pytest.fixture(scope="module")
def accs(gen):
    return _accumulate(gen)


@pytest.fixture(scope="module")
def depts(gen):
    return {d["department_id"]: DeptInfo(d["department_id"], d["name"], d["dept_type"], d["bed_capacity"], d["staff_count"]) for d in gen.departments}


def _run(job_type, accs, depts, start=START, end=END, failed=frozenset()):
    plan = make_plan(job_type, start, end, START, END)
    return compute_results(
        job_type=job_type, plan=plan, dept_ids=list(range(1, 9)), depts=depts, accs=accs, opening={}, failed_keys=set(failed)
    )


# ------------------------------------------------------------------- fold_row
def test_fold_row_counts_admissions_discharges_transfers_and_surgery():
    acc, trace = new_accumulator(), set()
    fold_row(acc, "admission", 4, "emergency", 10, None, None, trace)
    fold_row(acc, "icu_admit", 5, "transfer", 10, None, None, trace)
    fold_row(acc, "transfer", 3, "transfer", 11, None, None, trace)
    fold_row(acc, "discharge", 2, "scheduled", 12, 30.0, None, trace)
    fold_row(acc, "surgery", 3, "scheduled", 12, None, None, trace)
    assert (acc["n"], acc["adm"], acc["xfer"], acc["dis"], acc["surg"]) == (5, 2, 1, 1, 1)
    assert acc["sev_sum"] == 9 and acc["los_sum"] == 30.0 and acc["los_n"] == 1
    assert acc["hour_adm"][10] == 2 and acc["src"] == {"emergency": 1, "transfer": 1}
    assert acc["delta"][10] == 2 and acc["delta"][11] == 1 and acc["delta"][12] == -1  # surgery moves no bed


def test_fold_row_follows_traced_events_only():
    acc = new_accumulator()
    fold_row(acc, "admission", 3, "emergency", 1, None, "aaa", {"aaa"})
    fold_row(acc, "admission", 3, "emergency", 1, None, "bbb", {"aaa"})
    assert acc["traced"] == ["aaa"]


def test_accumulators_are_json_serialisable():
    import json

    acc = new_accumulator()
    fold_row(acc, "admission", 3, "emergency", 1, None, None, set())
    assert json.loads(json.dumps(acc)) == acc


# --------------------------------------------------------------------- reduce
def test_admission_rows_match_direct_aggregation(gen, accs, depts):
    res = _run("admission_patterns", accs, depts)
    ev = gen.events
    adm_types = [EVENT_TYPE_NAMES.index("admission"), EVENT_TYPE_NAMES.index("icu_admit")]
    direct = defaultdict(int)
    for t, d, ty in zip(ev["time"], ev["dept"], ev["type"]):
        if int(ty) in adm_types:
            direct[(date.fromordinal(EPOCH_ORDINAL + int(t // 86400)), int(d))] += 1
    assert len(res.admission_rows) == 8 * ((END - START).days + 1)
    for row in res.admission_rows:
        assert row["admission_count"] == direct[(row["metric_date"], row["department_id"])]
    assert res.events_processed == gen.count


def test_job_types_only_produce_their_own_tables(accs, depts):
    adm = _run("admission_patterns", accs, depts)
    assert adm.admission_rows and not adm.resource_rows and not adm.anomaly_rows
    res = _run("resource_utilisation", accs, depts)
    assert res.resource_rows and not res.admission_rows and not res.anomaly_rows
    full = _run("full_analysis", accs, depts)
    assert full.admission_rows and full.resource_rows


def test_hourly_occupancy_is_the_running_sum_of_bed_deltas(gen, accs, depts):
    res = _run("resource_utilisation", accs, depts)
    ev = gen.events
    up = [EVENT_TYPE_NAMES.index(n) for n in ("admission", "transfer", "icu_admit")]
    down = [EVENT_TYPE_NAMES.index(n) for n in ("discharge", "icu_discharge")]
    dept = 3
    m = ev["dept"] == dept
    times, types = ev["time"][m], ev["type"][m]
    delta = np.where(np.isin(types, up), 1, np.where(np.isin(types, down), -1, 0))
    rows = {(r["metric_date"], r["metric_hour"]): r for r in res.resource_rows if r["department_id"] == dept}
    for day_i in (5, 20, 40):
        d = START + timedelta(days=day_i)
        for hour in (0, 11, 23):
            cutoff = (d.toordinal() - EPOCH_ORDINAL) * 86400 + (hour + 1) * 3600
            assert rows[(d, hour)]["occupied_beds"] == max(int(delta[times < cutoff].sum()), 0)


def test_resource_rows_carry_capacity_rate_and_staffing(accs, depts):
    row = next(r for r in _run("resource_utilisation", accs, depts).resource_rows if r["department_id"] == 8)
    assert row["bed_capacity"] == depts[8].bed_capacity
    assert row["occupancy_rate"] == pytest.approx(row["occupied_beds"] / row["bed_capacity"], abs=1e-3)
    assert row["icu_occupancy_rate"] == row["occupancy_rate"]  # ICU only
    other = next(r for r in _run("resource_utilisation", accs, depts).resource_rows if r["department_id"] == 2)
    assert other["icu_occupancy_rate"] is None


def test_detector_finds_the_injected_mass_casualty(gen, accs, depts):
    res = _run("full_analysis", accs, depts)
    truth = next(a for a in gen.truth["anomalies"] if a["id"] == "mass_casualty")
    t0 = np.datetime64(truth["window_start"].replace("Z", ""))
    hits = [
        w for w in res.anomaly_rows
        if w["department_id"] == 1 and w["z_score"] > 0
        and np.datetime64(w["window_start"].replace(tzinfo=None)) < np.datetime64(truth["window_end"].replace("Z", ""))
        and np.datetime64(w["window_end"].replace(tzinfo=None)) > t0
    ]
    assert hits and max(w["z_score"] for w in hits) > 4


def test_detector_finds_the_equipment_outage_as_a_LOW_anomaly(gen, accs, depts):
    res = _run("full_analysis", accs, depts)
    truth = next(a for a in gen.truth["anomalies"] if a["id"] == "equipment_outage")
    lows = [w for w in res.anomaly_rows if w["department_id"] == 7 and w["z_score"] < 0]
    assert any(truth["window_start"][:10] <= w["window_start"].date().isoformat() <= truth["window_end"][:10] for w in lows)


def test_only_the_requested_window_is_written_but_lookback_feeds_the_baseline(accs, depts):
    start, end = date(2025, 2, 10), date(2025, 2, 20)
    res = _run("full_analysis", accs, depts, start=start, end=end)
    assert {r["metric_date"] for r in res.admission_rows} == {start + timedelta(days=i) for i in range(11)}
    assert all(start <= w["window_start"].date() <= end for w in res.anomaly_rows)


def test_a_failed_partition_withholds_that_departments_derived_results(accs, depts):
    bad = f"{(START + timedelta(days=10)).isoformat()}:3"
    broken = dict(accs)
    del broken[bad]
    res = _run("full_analysis", broken, depts, failed={bad})
    assert not [r for r in res.resource_rows if r["department_id"] == 3]
    assert not [w for w in res.anomaly_rows if w["department_id"] == 3]
    assert [r for r in res.resource_rows if r["department_id"] == 2]
    assert [r for r in res.admission_rows if r["department_id"] == 3]  # surviving days are still reported
    assert (START + timedelta(days=10), 3) not in {(r["metric_date"], r["department_id"]) for r in res.admission_rows}


def test_traced_events_are_reported_by_the_reducer(gen):
    accs = _accumulate(gen, trace={"x"})
    first = next(iter(accs.values()))
    first["traced"].append("trace-me")
    res = compute_results(
        job_type="admission_patterns", plan=make_plan("admission_patterns", START, END, START, END),
        dept_ids=list(range(1, 9)), depts={d["department_id"]: DeptInfo(d["department_id"], d["name"], d["dept_type"], d["bed_capacity"], d["staff_count"]) for d in gen.departments},
        accs=accs, opening={}, failed_keys=set(),
    )
    assert ("trace-me", next(iter(accs))) in res.traced
