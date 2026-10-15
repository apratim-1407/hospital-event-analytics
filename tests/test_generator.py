from datetime import date

import numpy as np
import pytest

from scripts.generate import EVENT_TYPE_NAMES, generate

START, END = date(2025, 1, 1), date(2025, 3, 31)


@pytest.fixture(scope="module")
def gen():
    return generate(15000, START, END, seed=7)


def test_event_count_is_exact(gen):
    assert gen.count == 15000


def test_same_seed_gives_identical_data():
    a = generate(3000, date(2025, 1, 1), date(2025, 1, 31), seed=3)
    b = generate(3000, date(2025, 1, 1), date(2025, 1, 31), seed=3)
    assert np.array_equal(a.events["time"], b.events["time"]) and np.array_equal(a.events["type"], b.events["type"])


def test_different_seed_gives_different_data():
    a = generate(3000, date(2025, 1, 1), date(2025, 1, 31), seed=3)
    b = generate(3000, date(2025, 1, 1), date(2025, 1, 31), seed=4)
    assert not np.array_equal(a.events["time"], b.events["time"])


def test_events_are_sorted_and_inside_the_period(gen):
    t = gen.events["time"]
    assert np.all(np.diff(t) >= 0)
    lo = np.datetime64(START.isoformat(), "s").astype("int64")
    hi = np.datetime64("2025-04-01", "s").astype("int64")
    assert t.min() >= lo and t.max() < hi


def test_occupancy_reconstructed_from_deltas_is_never_negative(gen):
    """Every departure has a matching earlier arrival, so beds-in-use never dips below zero."""
    ev = gen.events
    up = {EVENT_TYPE_NAMES.index(n) for n in ("admission", "transfer", "icu_admit")}
    down = {EVENT_TYPE_NAMES.index(n) for n in ("discharge", "icu_discharge")}
    for dept in range(1, 9):
        m = ev["dept"] == dept
        delta = np.where(np.isin(ev["type"][m], list(up)), 1, np.where(np.isin(ev["type"][m], list(down)), -1, 0))
        assert np.cumsum(delta).min() >= 0


def test_length_of_stay_only_on_discharges(gen):
    ev = gen.events
    has_los = ~np.isnan(ev["los"])
    discharge_types = [EVENT_TYPE_NAMES.index("discharge"), EVENT_TYPE_NAMES.index("icu_discharge")]
    assert np.all(np.isin(ev["type"][has_los], discharge_types))
    assert np.all(has_los[np.isin(ev["type"], discharge_types)])


def test_length_of_stay_is_right_skewed(gen):
    los = gen.events["los"]
    los = los[~np.isnan(los)]
    assert los.mean() > np.median(los)  # log-normal: the mean sits above the median


def test_severity_in_range_and_icu_is_sicker(gen):
    ev = gen.events
    assert ev["sev"].min() >= 1 and ev["sev"].max() <= 5
    assert ev["sev"][ev["dept"] == 8].mean() > ev["sev"][ev["dept"] == 6].mean()


def test_weekend_surgery_drops_sharply(gen):
    ev = gen.events
    surgery_arrivals = (ev["dept"] == 7) & (ev["type"] == EVENT_TYPE_NAMES.index("admission"))
    days = (ev["time"][surgery_arrivals] // 86400 + 3) % 7  # 1970-01-01 was a Thursday
    weekend = np.isin(days, [5, 6]).sum() / 2
    weekday = (~np.isin(days, [5, 6])).sum() / 5
    assert weekend < 0.7 * weekday


def test_ground_truth_lists_injected_anomalies_with_windows(gen):
    ids = {a["id"] for a in gen.truth["anomalies"]}
    assert {"mass_casualty", "icu_capacity_crisis", "equipment_outage"} <= ids
    for a in gen.truth["anomalies"]:
        assert a["window_start"] < a["window_end"] and a["direction"] in ("high", "low")


def test_icu_crisis_actually_exceeds_95_percent_of_capacity(gen):
    t = next(a for a in gen.truth["anomalies"] if a["id"] == "icu_capacity_crisis")
    ev = gen.events
    t0 = int(np.datetime64(t["window_start"].replace("Z", ""), "s").astype("int64"))
    t1 = int(np.datetime64(t["window_end"].replace("Z", ""), "s").astype("int64"))
    m = ev["dept"] == 8
    times, types = ev["time"][m], ev["type"][m]
    delta = np.where(types == EVENT_TYPE_NAMES.index("icu_admit"), 1, -1)
    cap = next(d["bed_capacity"] for d in gen.departments if d["department_id"] == 8)
    occ_at = lambda ts: int(delta[times <= ts].sum())  # noqa: E731
    assert min(occ_at(t0 + h * 3600) for h in range(48)) >= 0.95 * cap


def test_equipment_outage_empties_surgery(gen):
    t = next(a for a in gen.truth["anomalies"] if a["id"] == "equipment_outage")
    ev = gen.events
    t0 = int(np.datetime64(t["window_start"].replace("Z", ""), "s").astype("int64"))
    t1 = int(np.datetime64(t["window_end"].replace("Z", ""), "s").astype("int64"))
    arrivals = (ev["dept"] == 7) & (ev["type"] == EVENT_TYPE_NAMES.index("admission"))
    inside = ((ev["time"] >= t0) & (ev["time"] < t1) & arrivals).sum()
    assert inside <= 3


def test_hospital_scales_with_the_dataset():
    small = generate(5000, date(2025, 1, 1), date(2025, 1, 31), seed=1)
    large = generate(50000, date(2025, 1, 1), date(2025, 1, 31), seed=1)
    cap = lambda g: sum(d["bed_capacity"] for d in g.departments)  # noqa: E731
    assert cap(large) > 5 * cap(small)
