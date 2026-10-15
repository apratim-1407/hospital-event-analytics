"""Unusual-workload detection: rolling, seasonally-aware z-scores in BOTH directions.

The problem statement says unusual workload periods, not unusually high ones, so a
department that goes quiet (an equipment outage that stops surgery) is as
reportable as one that is overwhelmed.

Baseline. A point is compared against the previous `window` observations that share
its *slot*: same hour-of-day and same day-type (weekday/weekend) for admission counts,
same hour-of-day and same day-of-week for occupancy (a ward that fills during the week
and drains over the weekend has a genuine 7-day cycle: Saturday is not Sunday). That
removes the daily and weekly cycles a real hospital has, so the detector flags
departures from the expected pattern rather than the pattern itself.

Noise. With 168 day-of-week x hour slots there are only a handful of samples per slot,
far too few to estimate a standard deviation (a lucky tight cluster would turn ordinary
variation into a z of 10). For such series the mean stays per-slot but the noise level is
pooled across every slot (an exponentially-decayed mean of squared residuals).

Robustness. A flagged value is kept OUT of the baseline, so an anomaly cannot teach the
detector that it is normal (a doubled weekend must not make the next doubled weekend look
unremarkable). The exception is a genuine level shift: once a slot has been flagged for
`window // 2` consecutive observations the new level is accepted, winsorised to the
threshold, and the baseline migrates to it.
"""
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math

import numpy as np

UTC = timezone.utc


@dataclass(frozen=True)
class DetectorConfig:
    metric: str
    grain: str                      # "hour" | "day"
    threshold: float                # |z| at which a point is flagged
    min_run: int                    # consecutive flagged points needed (unless very extreme)
    window: int = 28                # baseline samples per slot
    min_samples: int = 5            # no verdict until a slot has this much history
    slot_kind: str = "daytype"      # "daytype" (weekday/weekend) | "dow" (day of week)
    pooled_std: bool = False        # estimate noise across all slots (few samples per slot)
    floor_kind: str = "poisson"     # std floor: sqrt(mean) for counts, constant for rates
    floor_value: float = 0.0
    high_z: float = 4.0
    critical_z: float = 6.0
    critical_level: float | None = None  # observed value at/above which a window is critical


DAILY_ADMISSIONS = DetectorConfig("daily_admissions", "day", threshold=3.0, min_run=1)
HOURLY_ADMISSIONS = DetectorConfig("hourly_admissions", "hour", threshold=4.0, min_run=2)
OCCUPANCY = DetectorConfig(
    "occupancy_rate", "hour", threshold=3.5, min_run=6, window=8, min_samples=4, slot_kind="dow",
    pooled_std=True, floor_kind="fixed", floor_value=0.03, critical_level=0.95,
)
ALL_DETECTORS = (DAILY_ADMISSIONS, HOURLY_ADMISSIONS, OCCUPANCY)


def build_slots(range_start: date, n_points: int, grain: str, kind: str = "daytype") -> np.ndarray:
    """Slot id per point: day-type or day-of-week, times 24 plus the hour for hourly series."""
    idx = np.arange(n_points)
    if grain == "hour":
        day, hour = idx // 24, idx % 24
    else:
        day, hour = idx, np.zeros(n_points, dtype=int)
    dow = (range_start.weekday() + day) % 7
    cls = dow if kind == "dow" else (dow >= 5).astype(int)
    return cls * 24 + hour


def rolling_zscores(values: np.ndarray, slots: np.ndarray, cfg: DetectorConfig) -> tuple[np.ndarray, np.ndarray]:
    """Return (z, expected) per point; NaN where the slot has too little history."""
    n = len(values)
    z = np.full(n, np.nan)
    expected = np.full(n, np.nan)
    # slot -> [deque, running sum, running sum of squares]
    state: dict[int, list] = {}
    thr = cfg.threshold
    accept_after = max(cfg.window // 2, 1)
    decay = 0.9995
    pool_ss = pool_w = 0.0
    pool_ready = 48.0
    for i in range(n):
        s = int(slots[i])
        x = float(values[i])
        st = state.get(s)
        if st is None:
            st = [deque(), 0.0, 0.0, 0]  # window, sum, sum of squares, consecutive flags
            state[s] = st
        dq = st[0]
        cnt = len(dq)
        xc: float | None = x
        if cnt >= cfg.min_samples:
            mean = st[1] / cnt
            if cfg.pooled_std:
                std = math.sqrt(pool_ss / pool_w) if pool_w >= pool_ready else None
            else:
                std = math.sqrt(max(st[2] / cnt - mean * mean, 0.0) * cnt / (cnt - 1))
            if std is None:
                # Still learning the noise level: no verdict yet, but learn from this residual.
                pool_ss = pool_ss * decay + (x - mean) ** 2
                pool_w = pool_w * decay + 1.0
            else:
                floor = math.sqrt(max(mean, 0.5)) if cfg.floor_kind == "poisson" else cfg.floor_value
                eff = max(std, floor, 1e-9)
                zi = (x - mean) / eff
                z[i] = zi
                expected[i] = mean
                if abs(zi) >= thr:
                    st[3] += 1
                    if st[3] > accept_after:   # sustained: accept the new level, but gradually
                        xc = min(max(x, mean - thr * eff), mean + thr * eff)
                    else:                      # an anomaly must not redefine "normal"
                        xc = None
                else:
                    st[3] = 0
                    if cfg.pooled_std:
                        pool_ss = pool_ss * decay + (x - mean) ** 2
                        pool_w = pool_w * decay + 1.0
        if xc is None:
            continue
        if cnt == cfg.window:
            old = dq.popleft()
            st[1] -= old
            st[2] -= old * old
        dq.append(xc)
        st[1] += xc
        st[2] += xc * xc
    return z, expected


def severity_label(max_abs_z: float, mean_observed: float, cfg: DetectorConfig) -> str:
    if cfg.critical_level is not None and mean_observed >= cfg.critical_level:
        return "critical"
    if max_abs_z >= cfg.critical_z:
        return "critical"
    if max_abs_z >= cfg.high_z:
        return "high"
    return "moderate"


def find_windows(
    values: np.ndarray,
    z: np.ndarray,
    expected: np.ndarray,
    cfg: DetectorConfig,
    range_start: date,
    first_valid_index: int = 0,
) -> list[dict]:
    """Merge consecutive same-direction flagged points into windows.

    `first_valid_index` drops points before the requested window (they were only
    mapped to build the baseline) and warm-up points while the dataset itself is
    still ramping up.
    """
    step = timedelta(hours=1) if cfg.grain == "hour" else timedelta(days=1)
    origin = datetime(range_start.year, range_start.month, range_start.day, tzinfo=UTC)
    flagged = np.abs(np.nan_to_num(z, nan=0.0)) >= cfg.threshold
    flagged[:first_valid_index] = False

    windows: list[dict] = []
    n = len(values)
    i = 0
    while i < n:
        if not flagged[i]:
            i += 1
            continue
        sign = 1 if z[i] > 0 else -1
        j = i
        while j + 1 < n and flagged[j + 1] and (1 if z[j + 1] > 0 else -1) == sign:
            j += 1
        run = slice(i, j + 1)
        max_abs = float(np.max(np.abs(z[run])))
        if (j - i + 1) >= cfg.min_run or max_abs >= cfg.critical_z:
            peak = i + int(np.argmax(np.abs(z[run])))
            mean_obs = float(np.mean(values[run]))
            windows.append(
                {
                    "window_start": origin + step * i,
                    "window_end": origin + step * (j + 1),
                    "metric_name": cfg.metric,
                    "observed_value": mean_obs,
                    "expected_value": float(np.nanmean(expected[run])),
                    "z_score": float(z[peak]),
                    "severity_label": severity_label(max_abs, mean_obs, cfg),
                }
            )
        i = j + 1
    return windows


def detect(values: np.ndarray, cfg: DetectorConfig, range_start: date, first_valid_index: int = 0) -> list[dict]:
    slots = build_slots(range_start, len(values), cfg.grain, cfg.slot_kind)
    z, expected = rolling_zscores(values, slots, cfg)
    return find_windows(values, z, expected, cfg, range_start, first_valid_index)
