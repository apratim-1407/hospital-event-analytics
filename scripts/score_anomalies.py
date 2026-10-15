"""Score detected anomaly windows against the generator's ground-truth file.

    python -m scripts.score_anomalies data/large_truth.json [--job-id <uuid>] [--verbose]

Recall:    every injected anomaly should be overlapped by a detected window in the right
           department, on a relevant metric, in the right direction.
Precision: of the windows the detector reported, how many correspond to an injected one.
           Reported for all severities and for high/critical only, since moderate windows
           in a 3-year, 8-department stream are expected to include chance deviations.
"""
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from app.core.db import engine

SLACK = timedelta(hours=1)
# Occupancy is a consequence: beds stay full for days (the length of stay) after the cause ends.
OCCUPANCY_TAIL = timedelta(days=4)


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_windows(job_id: str | None) -> list[dict]:
    sql = (
        "SELECT anomaly_id, department_id, metric_name, window_start, window_end, observed_value::float8 AS observed, "
        "expected_value::float8 AS expected, z_score::float8 AS z, severity_label FROM analytics.anomaly_windows"
    )
    params = {}
    if job_id:
        sql += " WHERE job_id = :j"
        params["j"] = job_id
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def matches(truth: dict, w: dict) -> bool:
    if w["department_id"] != truth["department_id"] or w["metric_name"] not in truth["metrics"]:
        return False
    if (w["z"] >= 0) != (truth["direction"] == "high"):
        return False
    tail = OCCUPANCY_TAIL if w["metric_name"] == "occupancy_rate" else SLACK
    lead = timedelta(hours=truth.get("lead_hours", 0))
    return w["window_start"] < _t(truth["window_end"]) + tail and w["window_end"] > _t(truth["window_start"]) - SLACK - lead


def score(truth_doc: dict, windows: list[dict]) -> dict:
    results = []
    for t in truth_doc["anomalies"]:
        hits = [w for w in windows if matches(t, w)]
        best = max(hits, key=lambda w: abs(w["z"]), default=None)
        results.append({"truth": t, "detected": bool(hits), "best": best, "n_windows": len(hits)})

    def is_true_positive(w):
        return any(matches(t, w) for t in truth_doc["anomalies"])

    strong = [w for w in windows if w["severity_label"] in ("high", "critical")]
    return {
        "recall": sum(r["detected"] for r in results) / max(len(results), 1),
        "per_anomaly": results,
        "windows_total": len(windows),
        "precision_all": sum(map(is_true_positive, windows)) / max(len(windows), 1),
        "windows_strong": len(strong),
        "precision_strong": sum(map(is_true_positive, strong)) / max(len(strong), 1),
        "false_positives_strong": [w for w in strong if not is_true_positive(w)],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("truth", type=Path)
    ap.add_argument("--job-id")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    truth = json.loads(args.truth.read_text())
    res = score(truth, load_windows(args.job_id))

    print(f"\nGround truth: {args.truth}  ({len(truth['anomalies'])} injected anomalies)\n")
    for r in res["per_anomaly"]:
        t, b = r["truth"], r["best"]
        flag = "FOUND  " if r["detected"] else "MISSED "
        extra = f"  best: {b['metric_name']} z={b['z']:+.1f} {b['severity_label']}" if b else ""
        print(f"  {flag}{t['id']:<22} dept {t['department_id']}  {t['window_start'][:16]} -> {t['window_end'][:16]}{extra}")
    print(
        f"\nRecall:            {res['recall']:.0%}"
        f"\nWindows reported:  {res['windows_total']}  (precision vs ground truth {res['precision_all']:.1%})"
        f"\nHigh/critical:     {res['windows_strong']}  (precision {res['precision_strong']:.1%})"
    )
    if args.verbose:
        print("\nHigh/critical windows not matching an injected anomaly:")
        for w in res["false_positives_strong"][:40]:
            print(f"  dept {w['department_id']} {w['metric_name']:<18} {w['window_start']:%Y-%m-%d %H:%M} -> {w['window_end']:%Y-%m-%d %H:%M} z={w['z']:+.1f} {w['severity_label']}")


if __name__ == "__main__":
    main()
