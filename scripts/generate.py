"""Synthetic hospital event generator with injected, recorded anomalies.

A uniform-random dataset would make every day identical and the anomaly detector would
find nothing, so the data carries real structure:

* Poisson arrivals, hourly rate peaking 10:00-12:00 and 18:00-21:00
* weekly seasonality: elective / surgical departments drop sharply at weekends
* annual seasonality: sinusoidal winter surge (general medicine, paediatrics)
* log-normal length of stay (short for most, long tail, so mean != median)
* severity correlation: higher triage severity lengthens the stay
* department-specific profiles (Emergency hot and short, ICU small and long, Surgery weekday-heavy)

Known anomalies are injected at known timestamps and written to a ground-truth file, so
the detector is *scored* against it rather than judged by eye.

Every stay produces an arrival event and, if the patient leaves inside the period, a
matching departure event, so bed occupancy reconstructed from event deltas is consistent.
Bed capacity per department is derived from the generated load (target utilisation), so
the hospital scales with the dataset tier.

    python -m scripts.generate --events 1000000 --start 2023-01-01 --end 2025-12-31 \\
        --seed 42 --out data/large.csv --anomalies data/large_truth.json
"""
import argparse
import csv
import json
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

HOURS = np.arange(24)
EVENT_TYPE_NAMES = ["admission", "discharge", "transfer", "icu_admit", "icu_discharge", "surgery"]
SOURCE_NAMES = ["emergency", "referral", "transfer", "scheduled"]
OVERGENERATE = 1.0  # baseline is thinned down to the exact target afterwards


def _g(h, mu, sd):
    return np.exp(-0.5 * ((h - mu) / sd) ** 2)


def _norm(v):
    return v / v.mean()


SHAPES = {
    "general": _norm(0.55 + 0.9 * _g(HOURS, 11, 1.6) + 0.7 * _g(HOURS, 19.5, 1.8)),
    "emergency": _norm(0.8 + 0.5 * _g(HOURS, 11, 2.5) + 0.6 * _g(HOURS, 19.5, 2.5)),
    "elective": _norm(0.04 + 1.0 * _g(HOURS, 9.5, 2.0) + 0.8 * _g(HOURS, 13.5, 2.0)),
    "flat": np.ones(24),
}


@dataclass(frozen=True)
class DeptProfile:
    id: int
    name: str
    type: str
    prefix: str
    rate: float            # stays per day at scale 1.0
    los_median: float      # hours
    los_sigma: float
    util: float            # target mean utilisation, used to size the ward
    staff_per_bed: float
    weekend_factor: float
    seasonal_amp: float
    shape: str
    bed_type: str
    severity_p: tuple
    source_p: tuple        # emergency, referral, transfer, scheduled
    procedure_p: float = 0.0  # chance a stay includes a surgical procedure event


PROFILES = [
    DeptProfile(1, "Emergency", "emergency", "E", 110, 5, 0.6, 0.55, 1.2, 1.05, 0.08, "emergency", "ed_bay",
                (.10, .20, .30, .25, .15), (.88, .04, .08, 0)),
    DeptProfile(2, "General Medicine", "ward", "G", 30, 60, 0.7, 0.65, 0.75, 0.90, 0.30, "general", "standard",
                (.10, .25, .35, .20, .10), (.55, .30, .15, 0)),
    DeptProfile(3, "Cardiology", "ward", "C", 8, 72, 0.6, 0.65, 1.0, 0.70, 0.10, "general", "standard",
                (.05, .15, .30, .30, .20), (.35, .40, .15, .10), 0.15),
    DeptProfile(4, "Orthopaedics", "ward", "O", 7, 72, 0.6, 0.65, 0.9, 0.45, 0.0, "elective", "standard",
                (.20, .35, .30, .10, .05), (.15, .15, .05, .65), 0.40),
    DeptProfile(5, "Paediatrics", "ward", "P", 10, 48, 0.6, 0.65, 1.0, 0.95, 0.25, "general", "standard",
                (.20, .30, .30, .15, .05), (.60, .30, .10, 0)),
    DeptProfile(6, "Maternity", "ward", "M", 7, 48, 0.5, 0.65, 1.0, 0.95, 0.0, "flat", "standard",
                (.30, .35, .20, .10, .05), (.25, .25, 0, .50)),
    DeptProfile(7, "Surgery", "surgical", "S", 12, 36, 0.6, 0.65, 1.1, 0.40, 0.0, "elective", "surgical",
                (.15, .30, .30, .15, .10), (.20, .10, 0, .70), 1.0),
    DeptProfile(8, "ICU", "icu", "I", 3.2, 96, 0.7, 0.62, 2.2, 0.95, 0.05, "flat", "icu",
                (0, 0, .15, .40, .45), (.35, .05, .60, 0)),
]
BY_ID = {p.id: p for p in PROFILES}


# ------------------------------------------------------------------------ stays
@dataclass
class Stays:
    dept: np.ndarray
    arr: np.ndarray        # unix seconds
    los: np.ndarray        # hours
    sev: np.ndarray
    src: np.ndarray        # index into SOURCE_NAMES
    proc: np.ndarray       # has a surgical procedure event
    injected: np.ndarray   # injected anomaly stay (never thinned)

    def __len__(self):
        return len(self.arr)

    @staticmethod
    def empty():
        z = np.array([], dtype=np.int64)
        return Stays(z.copy(), z.copy(), np.array([]), z.copy(), z.copy(), np.array([], dtype=bool), np.array([], dtype=bool))

    def take(self, mask):
        return Stays(*(getattr(self, f)[mask] for f in ("dept", "arr", "los", "sev", "src", "proc", "injected")))

    @staticmethod
    def concat(parts):
        parts = [p for p in parts if len(p)]
        if not parts:
            return Stays.empty()
        return Stays(*(np.concatenate([getattr(p, f) for p in parts]) for f in
                       ("dept", "arr", "los", "sev", "src", "proc", "injected")))


def make_stays(rng, dept: DeptProfile, arrivals: np.ndarray, injected=False, severity_p=None) -> Stays:
    n = len(arrivals)
    if n == 0:
        return Stays.empty()
    sev = rng.choice(5, size=n, p=severity_p or dept.severity_p) + 1
    los = np.exp(math.log(dept.los_median) + 0.15 * (sev - 3) + dept.los_sigma * rng.standard_normal(n))
    los = np.clip(los, 0.25, 24 * 45)
    src = rng.choice(4, size=n, p=dept.source_p)
    proc = rng.random(n) < dept.procedure_p
    return Stays(
        np.full(n, dept.id, dtype=np.int64), arrivals.astype(np.int64), los, sev.astype(np.int64),
        src.astype(np.int64), proc, np.full(n, injected, dtype=bool),
    )


def lambda_matrix(dept: DeptProfile, days: list[date], scale: float) -> np.ndarray:
    """Expected arrivals per (day, hour) for one department."""
    dow = np.array([d.weekday() for d in days])
    doy = np.array([d.timetuple().tm_yday for d in days])
    weekly = np.where(dow >= 5, dept.weekend_factor, 1.0)
    annual = 1.0 + dept.seasonal_amp * np.cos(2 * np.pi * (doy - 15) / 365.0)
    daily = dept.rate * scale * weekly * annual
    return daily[:, None] * SHAPES[dept.shape][None, :] / 24.0


def sample_arrivals(rng, lam: np.ndarray, day0_secs: int) -> np.ndarray:
    """Poisson counts per (day, hour) -> unix-second arrival times within each hour."""
    counts = rng.poisson(lam)
    flat = counts.ravel()
    total = int(flat.sum())
    hour_starts = day0_secs + np.arange(lam.size, dtype=np.int64) * 3600
    return np.repeat(hour_starts, flat) + rng.integers(0, 3600, size=total)


# ------------------------------------------------------------------- anomalies
def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def plan_anomalies(span_days: int, start: date) -> list[dict]:
    """Where each anomaly goes, as fractions of the period (and only when the period is
    long enough for the detector to have a baseline)."""
    plans = []
    if span_days >= 28:
        d = _clip(int(0.62 * span_days), 21, span_days - 3)
        plans.append(dict(id="mass_casualty", type="mass_casualty", dept=1, start_day=d, start_hour=14, hours=6,
                          direction="high", metrics=["hourly_admissions", "daily_admissions", "occupancy_rate"],
                          description="4x emergency admissions for 6 hours"))
        d = _clip(int(0.45 * span_days), 21, span_days - 4)
        plans.append(dict(id="icu_capacity_crisis", type="icu_crisis", dept=8, start_day=d, start_hour=6, hours=48,
                          direction="high", metrics=["occupancy_rate", "daily_admissions", "hourly_admissions"],
                          description="ICU occupancy above 95% for 48 hours"))
        d = _clip(int(0.72 * span_days), 21, span_days - 3)
        while (start + timedelta(days=d)).weekday() > 2 and d < span_days - 3:  # begin Mon-Wed
            d += 1
        plans.append(dict(id="equipment_outage", type="outage", dept=7, start_day=d, start_hour=0, hours=48,
                          direction="low", metrics=["daily_admissions", "hourly_admissions"],
                          description="Surgical admissions near zero for 2 days (unusually LOW workload)"))
    if span_days >= 120:
        d = _clip(int(0.83 * span_days), 30, span_days - 22)
        plans.append(dict(id="seasonal_flu_surge", type="flu", dept=2, start_day=d, start_hour=0, hours=21 * 24,
                          direction="high", metrics=["daily_admissions", "occupancy_rate"],
                          description="+80% general medicine admissions for 3 weeks"))
    if span_days >= 90:
        for i, frac in enumerate((0.30, 0.50, 0.90), start=1):
            d = _clip(int(frac * span_days), 21, span_days - 3)
            while (start + timedelta(days=d)).weekday() != 5 and d < span_days - 3:  # next Saturday
                d += 1
            plans.append(dict(id=f"weekend_overload_{i}", type="weekend", dept=5, start_day=d, start_hour=0, hours=48,
                              direction="high", metrics=["daily_admissions", "hourly_admissions", "occupancy_rate"],
                              description="Arrivals triple over a weekend, roughly doubling patients per staff member"))
    return plans


def _occupancy_hourly(stays: Stays, dept_id: int, t0: int, n_hours: int) -> np.ndarray:
    m = stays.dept == dept_id
    arr = (stays.arr[m] - t0) // 3600
    dep = (stays.arr[m] + np.round(stays.los[m] * 3600).astype(np.int64) - t0) // 3600
    delta = np.zeros(n_hours + 2)
    np.add.at(delta, np.clip(arr, 0, n_hours + 1), 1)
    np.add.at(delta, np.clip(dep, 0, n_hours + 1), -1)
    return np.cumsum(delta)[:n_hours]


# ------------------------------------------------------------------- generation
@dataclass
class Generated:
    events: dict
    departments: list[dict]
    beds: list[dict]
    truth: dict
    stats: dict = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.events["time"])


def _build_stays(seed, attempt, scale, days, start_secs, end_secs, anomaly_plans, with_anomalies=True):
    rng = np.random.default_rng([seed, attempt])
    n_days = len(days)
    baseline = []
    lam_cache = {}
    for dept in PROFILES:
        lam = lambda_matrix(dept, days, scale)
        lam_cache[dept.id] = lam
        arrivals = sample_arrivals(rng, lam, start_secs)
        arrivals = arrivals[arrivals < end_secs]
        baseline.append(make_stays(rng, dept, arrivals))
    base = Stays.concat(baseline)

    # Ward sizes follow the generated load. A hospital sizes wards for its busy hours, not its
    # average, so capacity is the larger of (mean occupancy / target utilisation) and (the busiest
    # ordinary hour / 92%): everyday variation stays inside the walls, and only genuine incidents
    # (the injected anomalies) push a ward to or past capacity.
    span_hours = n_days * 24
    warmup = 21 * 24 if n_days > 45 else 0  # the first weeks fill up from empty beds
    capacity = {}
    for dept in PROFILES:
        m = base.dept == dept.id
        mean_occ = base.los[m].sum() / span_hours
        occ = _occupancy_hourly(base, dept.id, start_secs, span_hours)[warmup:]
        busiest = float(np.percentile(occ, 99.9)) if len(occ) else mean_occ
        capacity[dept.id] = max(10, math.ceil(mean_occ / dept.util), math.ceil(busiest / 0.92))

    injected = []
    truth = []
    if with_anomalies:
        for plan in anomaly_plans:
            dept = BY_ID[plan["dept"]]
            t_start = start_secs + plan["start_day"] * 86400 + plan["start_hour"] * 3600
            t_end = t_start + plan["hours"] * 3600
            lam = lam_cache[dept.id]
            flat = lam.ravel()  # per absolute hour since start
            h0 = (t_start - start_secs) // 3600
            window_lam = flat[h0 : h0 + plan["hours"]]

            if plan["type"] == "mass_casualty":
                extra = rng.poisson(window_lam * 3.0)  # on top of baseline => ~4x
                times = np.repeat(t_start + np.arange(plan["hours"]) * 3600, extra) + rng.integers(0, 3600, int(extra.sum()))
                injected.append(make_stays(rng, dept, times, True, severity_p=(0, .05, .20, .40, .35)))
            elif plan["type"] == "flu":
                extra = rng.poisson(window_lam * 0.8)
                times = np.repeat(t_start + np.arange(plan["hours"]) * 3600, extra) + rng.integers(0, 3600, int(extra.sum()))
                injected.append(make_stays(rng, dept, times, True))
            elif plan["type"] == "weekend":
                # Staffing is fixed, so "patients per staff doubles" needs more than doubled
                # arrivals: beds fill gradually over a stay, so tripled arrivals over two days
                # roughly doubles occupancy.
                extra = rng.poisson(window_lam * 2.0)
                times = np.repeat(t_start + np.arange(plan["hours"]) * 3600, extra) + rng.integers(0, 3600, int(extra.sum()))
                injected.append(make_stays(rng, dept, times, True))
            elif plan["type"] == "outage":
                # Remove (almost all of) the department's own arrivals in the window.
                m = (base.dept == dept.id) & (base.arr >= t_start) & (base.arr < t_end)
                keep_some = rng.random(len(base)) < 0.03
                drop = m & ~keep_some
                base = base.take(~drop)
            elif plan["type"] == "icu_crisis":
                n_hours = n_days * 24
                occ = _occupancy_hourly(base, dept.id, start_secs, n_hours)
                win = occ[h0 : h0 + plan["hours"]]
                target = math.ceil(0.975 * capacity[dept.id])
                need = int(max(0, target - win.min()))
                times = t_start - rng.integers(0, 8 * 3600, need)
                s = make_stays(rng, dept, times, True)
                s.los = rng.uniform(62, 85, need)  # outlast the 48h window
                s.sev[:] = 5
                injected.append(s)
            truth.append(
                {
                    "id": plan["id"],
                    "type": plan["type"],
                    "department_id": dept.id,
                    "department": dept.name,
                    "window_start": datetime.fromtimestamp(t_start, UTC).isoformat().replace("+00:00", "Z"),
                    "window_end": datetime.fromtimestamp(t_end, UTC).isoformat().replace("+00:00", "Z"),
                    "direction": plan["direction"],
                    "metrics": plan["metrics"],
                    # The ICU surge is built from admissions in the hours before the window opens.
                    "lead_hours": 8 if plan["type"] == "icu_crisis" else 0,
                    "description": plan["description"],
                }
            )
    return base, Stays.concat(injected), capacity, truth, rng


def _departure_secs(stays: Stays) -> np.ndarray:
    return stays.arr + np.round(stays.los * 3600).astype(np.int64)


def _procedure_secs(stays: Stays) -> np.ndarray:
    """A surgical procedure happens a fifth of the way through the stay (always inside it)."""
    return stays.arr + np.round(stays.los * 3600 * 0.2).astype(np.int64)


def _events_per_stay(stays: Stays, end_secs: int) -> np.ndarray:
    n = 1 + (_departure_secs(stays) < end_secs).astype(np.int64)
    return n + (stays.proc & (_procedure_secs(stays) < end_secs)).astype(np.int64)


def generate(events: int, start: date, end: date, seed: int = 42, anomalies: bool = True) -> Generated:
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    n_days = len(days)
    start_secs = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp())
    end_secs = start_secs + n_days * 86400
    plans = plan_anomalies(n_days, start)

    # Calibrate the hospital's size so the expected event count matches the target.
    unit_per_day = sum(p.rate * (2 + p.procedure_p) for p in PROFILES)
    scale = events / (unit_per_day * n_days * 0.97) * OVERGENERATE
    for attempt in range(8):
        base, inj, capacity, truth, rng = _build_stays(seed, attempt, scale, days, start_secs, end_secs, plans, anomalies)
        total = int(_events_per_stay(base, end_secs).sum() + (_events_per_stay(inj, end_secs).sum() if len(inj) else 0))
        if total >= events:
            break
        scale *= (events / max(total, 1)) * 1.02

    # Thin baseline stays (never ICU, never injected) to land exactly on the target.
    ec = _events_per_stay(base, end_secs)
    excess = total - events
    if excess > 0:
        candidates = np.flatnonzero(base.dept != 8)
        order = rng.permutation(candidates)
        cum = np.cumsum(ec[order])
        k = int(np.searchsorted(cum, excess, side="right"))  # largest prefix with cum <= excess
        remove = np.zeros(len(base), dtype=bool)
        remove[order[:k]] = True
        removed = int(cum[k - 1]) if k else 0
        residual = excess - removed
        # Shave any 1-2 leftover events by dropping surgical procedure events.
        if residual > 0:
            procs = np.flatnonzero(base.proc & ~remove & (_procedure_secs(base) < end_secs))
            for i in procs[:residual]:
                base.proc[i] = False
                residual -= 1
        base = base.take(~remove)
    stays = Stays.concat([base, inj])

    ev = _stays_to_events(rng, stays, capacity, end_secs)
    gen_departments = [
        {
            "department_id": p.id, "name": p.name, "dept_type": p.type,
            "bed_capacity": capacity[p.id], "staff_count": max(1, math.ceil(capacity[p.id] * p.staff_per_bed)),
        }
        for p in PROFILES
    ]
    beds = []
    for p in PROFILES:
        for n in range(1, capacity[p.id] + 1):
            beds.append({"bed_id": f"{p.prefix}-{n:03d}", "department_id": p.id, "bed_type": p.bed_type})

    truth_doc = {
        "seed": seed, "target_events": events, "start": start.isoformat(), "end": end.isoformat(),
        "anomalies": truth,
    }
    return Generated(
        events=ev, departments=gen_departments, beds=beds, truth=truth_doc,
        stats={"scale": round(scale, 3), "stays": len(stays), "events": len(ev["time"]), "capacity": capacity},
    )


def _stays_to_events(rng, stays: Stays, capacity: dict, end_secs: int) -> dict:
    n = len(stays)
    patient = rng.integers(1, max(500, n // 3) + 1, n)
    cap_by_stay = np.array([capacity[int(d)] for d in stays.dept], dtype=np.int64)
    bed_no = (rng.random(n) * cap_by_stay).astype(np.int64) + 1
    prefix = {p.id: p.prefix for p in PROFILES}
    bed = np.array([f"{prefix[int(d)]}-{int(b):03d}" for d, b in zip(stays.dept, bed_no)], dtype=object)

    is_icu = stays.dept == 8
    arr_type = np.where(is_icu, 3, np.where(stays.src == 2, 2, 0))  # icu_admit / transfer / admission
    dep_type = np.where(is_icu, 4, 1)                               # icu_discharge / discharge
    dep_secs = _departure_secs(stays)
    dep_ok = dep_secs < end_secs
    proc_secs = _procedure_secs(stays)
    proc_ok = stays.proc & (proc_secs < end_secs)

    def part(mask, t, etype, los):
        k = int(mask.sum())
        return dict(
            time=t[mask],
            type=(etype[mask] if isinstance(etype, np.ndarray) else np.full(k, etype)),
            dept=stays.dept[mask], sev=stays.sev[mask], src=stays.src[mask], patient=patient[mask],
            bed=bed[mask], los=(los[mask] if los is not None else np.full(k, np.nan)),
        )

    every = np.ones(n, dtype=bool)
    parts = [
        part(every, stays.arr, arr_type, None),
        part(dep_ok, dep_secs, dep_type, np.round(stays.los, 2)),
        part(proc_ok, proc_secs, 5, None),  # procedures are not bed events and carry no stay length
    ]
    out = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    order = np.lexsort((out["type"], out["time"]))
    return {k: v[order] for k, v in out.items()}


# ----------------------------------------------------------------------- output
CSV_HEADER = ["patient_ref", "event_type", "department_id", "event_time", "severity",
              "admission_source", "bed_id", "length_of_stay_hours"]


def write_events_csv(ev: dict, path: Path) -> None:
    times = np.datetime_as_string(ev["time"].astype("datetime64[s]"), unit="s", timezone="UTC")
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(CSV_HEADER)
        types, srcs = ev["type"], ev["src"]
        for i in range(len(times)):
            los = ev["los"][i]
            w.writerow(
                (
                    f"p_{int(ev['patient'][i])}", EVENT_TYPE_NAMES[int(types[i])], int(ev["dept"][i]), times[i],
                    int(ev["sev"][i]), SOURCE_NAMES[int(srcs[i])], ev["bed"][i],
                    "" if np.isnan(los) else f"{los:.2f}",
                )
            )


def write_reference_csvs(gen: Generated, out: Path) -> tuple[Path, Path]:
    dpath = out.with_name(out.stem + "_departments.csv")
    bpath = out.with_name(out.stem + "_beds.csv")
    with dpath.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["department_id", "name", "dept_type", "bed_capacity", "staff_count"], lineterminator="\n")
        w.writeheader()
        w.writerows(gen.departments)
    with bpath.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["bed_id", "department_id", "bed_type"], lineterminator="\n")
        w.writeheader()
        w.writerows(gen.beds)
    return dpath, bpath


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", type=int, required=True, help="exact number of events to generate")
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True, help="inclusive")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--anomalies", type=Path, help="ground-truth JSON output path")
    ap.add_argument("--no-anomalies", action="store_true")
    args = ap.parse_args()

    t0 = time.perf_counter()
    gen = generate(args.events, args.start, args.end, args.seed, anomalies=not args.no_anomalies)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_events_csv(gen.events, args.out)
    dpath, bpath = write_reference_csvs(gen, args.out)
    if args.anomalies:
        args.anomalies.write_text(json.dumps(gen.truth, indent=2))
    size_mb = args.out.stat().st_size / 1e6
    print(
        f"generated {gen.count:,} events ({size_mb:.1f} MB) in {time.perf_counter() - t0:.1f}s\n"
        f"  events:       {args.out}\n  departments:  {dpath}\n  beds:         {bpath}"
        + (f"\n  ground truth: {args.anomalies} ({len(gen.truth['anomalies'])} anomalies)" if args.anomalies else "")
    )


if __name__ == "__main__":
    main()
