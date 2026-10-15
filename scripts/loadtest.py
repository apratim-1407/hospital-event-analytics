"""Run the Locust workload levels and collect the HTML reports + a comparison table.

    python scripts/loadtest.py                     # low, medium, high, breaking at 5 min each
    python scripts/loadtest.py --duration 90s      # quicker
    python scripts/loadtest.py --levels low medium

Levels (spawn rates from the project design):
    low       10 users   2/s     baseline, all green
    medium   100 users  10/s     p95 rises, errors stay at zero
    high     500 users  25/s     latency degrades; some 429s from the ingest rate limiter
    breaking 1000 users 50/s     find the actual bottleneck

Run on the host; each level runs in the `tools` container against http://api:8000.
Reports: benchmarks/locust_<level>.html and benchmarks/locust_<level>_stats.csv
"""
import argparse
import csv
import json
import sys

import hostctl as h

LEVELS = {
    "low": (10, 2),
    "medium": (100, 10),
    "high": (500, 25),
    "breaking": (1000, 50),
}


def run_level(name: str, users: int, rate: int, duration: str, data_start: str, data_days: int) -> dict:
    h.say(f"\n== {name}: {users} users, spawn {rate}/s, {duration} ==")
    prefix = f"benchmarks/locust_{name}"
    h.compose(
        "--profile", "tools", "run", "--rm",
        "-e", f"LOAD_DATA_START={data_start}", "-e", f"LOAD_DATA_DAYS={data_days}",
        "tools", "locust", "-f", "loadtests/locustfile.py", "--host", "http://api:8000", "--headless",
        "-u", str(users), "-r", str(rate), "-t", duration, "--html", f"{prefix}.html", "--csv", prefix, "--only-summary",
        check=False,
    )
    rows = list(csv.DictReader((h.ROOT / f"{prefix}_stats.csv").open(encoding="utf-8")))
    agg = next(r for r in rows if r["Name"] == "Aggregated")
    by_ep = {r["Name"]: r for r in rows if r["Name"] != "Aggregated"}
    return {
        "level": name, "users": users,
        "requests": int(agg["Request Count"]), "failures": int(agg["Failure Count"]),
        "rps": round(float(agg["Requests/s"]), 1),
        "p50_ms": float(agg["50%"]), "p95_ms": float(agg["95%"]), "p99_ms": float(agg["99%"]),
        "error_rate_pct": round(100 * int(agg["Failure Count"]) / max(int(agg["Request Count"]), 1), 2),
        "endpoints": {k: {"reqs": int(v["Request Count"]), "fails": int(v["Failure Count"]), "p50": float(v["50%"]), "p95": float(v["95%"])} for k, v in by_ep.items()},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--levels", nargs="+", default=list(LEVELS), choices=list(LEVELS))
    ap.add_argument("--duration", default="5m")
    ap.add_argument("--data-start", default="2025-03-01", help="first day Locust queries")
    ap.add_argument("--data-days", type=int, default=60)
    args = ap.parse_args()

    if h.http("GET", "/health")[0] != 200:
        sys.exit("API is not healthy; start the stack first")
    results = [run_level(n, *LEVELS[n], args.duration, args.data_start, args.data_days) for n in args.levels]

    (h.ROOT / "benchmarks" / "loadtest_results.json").write_text(json.dumps(results, indent=2))
    lines = ["| Level | Users | Requests | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Error rate |", "|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['level']} | {r['users']} | {r['requests']:,} | {r['rps']} | {r['p50_ms']:.0f} | {r['p95_ms']:.0f} | {r['p99_ms']:.0f} | {r['error_rate_pct']}% |")
    table = "\n".join(lines)
    (h.ROOT / "benchmarks" / "loadtest_results.md").write_text(table + "\n")
    h.say("\n" + table)


if __name__ == "__main__":
    main()
