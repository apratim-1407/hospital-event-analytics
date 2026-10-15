from datetime import date

import numpy as np

from app.pipeline import anomaly
from app.pipeline.anomaly import DetectorConfig, build_slots, detect

START = date(2025, 1, 6)  # a Monday
DAILY = DetectorConfig("daily_admissions", "day", threshold=3.0, min_run=1)


def _series(n_days=120, weekday=50.0, weekend=30.0, seed=0):
    rng = np.random.default_rng(seed)
    base = np.array([weekend if ((START.weekday() + i) % 7) >= 5 else weekday for i in range(n_days)])
    return rng.poisson(base).astype(float)


def test_slots_separate_weekends_from_weekdays():
    slots = build_slots(START, 14, "day")
    assert list(slots[:7]) == [0, 0, 0, 0, 0, 24, 24]  # Mon..Fri weekday slot, Sat/Sun weekend slot


def test_hourly_slots_include_the_hour():
    slots = build_slots(START, 48, "hour")
    assert slots[0] == 0 and slots[13] == 13 and slots[24] == 0


def test_dow_slots_distinguish_saturday_from_sunday():
    slots = build_slots(START, 7, "day", "dow")
    assert len(set(slots.tolist())) == 7


def test_quiet_series_raises_few_flags():
    values = _series()
    windows = detect(values, DAILY, START)
    assert len(windows) <= 3  # ~0.3% two-sided tail per day at 3 sigma


def test_spike_is_flagged_high():
    values = _series()
    values[80] *= 3
    windows = detect(values, DAILY, START)
    hit = [w for w in windows if w["window_start"].date() == date.fromordinal(START.toordinal() + 80)]
    assert hit and hit[0]["z_score"] > 3


def test_drop_is_flagged_low_not_ignored():
    """Unusual workload means high OR low: an outage that empties a department counts."""
    values = _series()
    values[70] = 0
    windows = detect(values, DAILY, START)
    low = [w for w in windows if w["z_score"] < 0 and w["window_start"].date() == date.fromordinal(START.toordinal() + 70)]
    assert low


def test_weekend_dip_is_normal_not_an_anomaly():
    """A seasonal pattern is the baseline, not a deviation from it."""
    values = _series(weekday=100, weekend=30)
    flagged = [w for w in detect(values, DAILY, START) if abs(w["z_score"]) > 3]
    assert len(flagged) <= 3


def test_consecutive_days_merge_into_one_window():
    values = _series()
    values[80:83] *= 3
    windows = detect(values, DAILY, START)
    merged = [w for w in windows if (w["window_end"] - w["window_start"]).days >= 3]
    assert merged


def test_flagged_values_do_not_poison_the_baseline():
    """Two identical spikes a week apart must both be caught: the first must not teach the
    detector that spikes are normal."""
    values = _series()
    values[70] *= 3
    values[77] *= 3
    starts = {w["window_start"].date().toordinal() - START.toordinal() for w in detect(values, DAILY, START)}
    assert 70 in starts and 77 in starts


def test_sustained_shift_is_eventually_accepted_as_the_new_normal():
    values = _series(n_days=200)
    values[60:] *= 1.6  # permanent level shift (e.g. a new ward opens)
    windows = detect(values, DAILY, START)
    late = [w for w in windows if w["window_start"].date().toordinal() - START.toordinal() > 150]
    assert not late


def test_first_valid_index_suppresses_lookback_flags():
    values = _series()
    values[50] *= 3
    assert detect(values, DAILY, START, first_valid_index=60) == []


def test_min_run_filters_single_point_noise_but_not_extremes():
    cfg = DetectorConfig("hourly_admissions", "hour", threshold=3.0, min_run=3)
    rng = np.random.default_rng(1)
    values = rng.poisson(10, 24 * 40).astype(float)
    values[24 * 30 + 5] = 22  # moderate single-hour blip: dropped
    values[24 * 35 + 5] = 80  # extreme: kept even as a single point
    starts = {w["window_start"].hour + 24 * (w["window_start"].date().toordinal() - START.toordinal()) for w in detect(values, cfg, START)}
    assert 24 * 35 + 5 in starts
    assert 24 * 30 + 5 not in starts


def test_occupancy_at_capacity_is_critical():
    cfg = anomaly.OCCUPANCY
    rng = np.random.default_rng(2)
    occ = np.clip(rng.normal(0.6, 0.04, 24 * 60), 0, 1)
    occ[24 * 50 : 24 * 52] = 0.99
    windows = detect(occ, cfg, START)
    crit = [w for w in windows if w["severity_label"] == "critical"]
    assert crit and any(w["observed_value"] >= 0.95 for w in crit)


def test_severity_labels_scale_with_z():
    cfg = DAILY
    assert anomaly.severity_label(3.2, 10, cfg) == "moderate"
    assert anomaly.severity_label(4.5, 10, cfg) == "high"
    assert anomaly.severity_label(7.0, 10, cfg) == "critical"
