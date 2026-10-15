"""Benchmark the processing tier: the headline data-intensive experiment.

Runs the same full-analysis job over the 10k / 100k / 1M datasets at different worker
counts and records processing time (metric M1), throughput, speed-up, and the query-side
p95 latency of GET /analytics/admissions at each data size (it should barely move: that
endpoint reads pre-aggregated tables whose size does not depend on raw volume).

    python scripts/benchmark.py                          # all sizes at 1 and 4 workers
    python scripts/benchmark.py --sizes large --workers 1 2 4   # the scaling experiment
    python scripts/benchmark.py --sizes small medium --workers 1 --repeat 3

Requires: the stack running (docker compose up -d --build) and datasets in ./data
(see scripts/generate.py). Results: benchmarks/results.json and benchmarks/results.md
"""
import argparse
import json
import platform
import statistics
import sys
from datetime import UTC, date, datetime, timedelta

import hostctl as h

SIZES = {"small": 10_000, "medium": 100_000, "large": 1_000_000}


def load_dataset(size: str) -> dict:
    csv = h.ROOT / "data" / f"{size}.csv"
    if not csv.exists():
        sys.exit(f"missing {csv}; generate it first (see README: Generate data)")
    h.say(f"\n== loading {size} ({SIZES[size]:,} events) ==")
    out = h.compose("--profile", "tools", "run", "--rm", "tools", "python", "-m", "scripts.load_csv", f"data/{size}.csv", "--truncate")
    line = [ln for ln in out.stdout.splitlines() if ln.startswith("{")][-1]
    info = json.loads(line)
    h.say(f"   loaded {info['rows']:,} rows in {info['seconds']}s ({info['rows_per_second']:,} rows/s)")
    return info


def data_range(size: str) -> tuple[str, str]:
    truth = json.loads((h.ROOT / "data" / f"{size}_truth.json").read_text())
    return truth["start"], truth["end"]


def run_once(token: str, size: str, workers: int) -> dict:
    start, end = data_range(size)
    accepted = h.submit_job(token, "full_analysis", start, end)
    h.say(f"   job {accepted['job_id'][:8]} accepted: {accepted['partitions_total']:,} partitions")
    last = {"pct": -10}

    def progress(job):
        if job["progress_pct"] - last["pct"] >= 25:
            last["pct"] = job["progress_pct"]
            h.say(f"     {job['progress_pct']:5.1f}%  ({job['partitions_completed']:,}/{job['partitions_total']:,})")

    job = h.wait_for_job(token, accepted["job_id"], on_poll=progress)
    if job["status"] != "completed":
        sys.exit(f"job ended {job['status']}: {job['error_detail']}")
    return {
        "size": size, "workers": workers, "events": job["events_processed"], "partitions": job["partitions_total"],
        "seconds": job["duration_seconds"], "events_per_second": round(job["events_processed"] / job["duration_seconds"]),
    }


def environment() -> dict:
    cpus = h.compose("exec", "-T", "postgres", "nproc").stdout.strip()
    return {
        "date": datetime.now(UTC).isoformat(timespec="seconds"), "host": platform.platform(),
        "docker_cpus": cpus, "worker_concurrency_per_container": 2,
    }


def render_markdown(results: list[dict], latency: dict, env: dict) -> str:
    lines = ["# Benchmark results", "", f"Recorded {env['date']} on {env['host']} ({env['docker_cpus']} CPUs visible to Docker). "
             f"Each worker container runs {env['worker_concurrency_per_container']} processes.", ""]
    by = {}
    for r in results:
        by.setdefault((r["size"], r["workers"]), []).append(r)
    sizes = [s for s in SIZES if any(k[0] == s for k in by)]
    worker_counts = sorted({k[1] for k in by})
    base_w = worker_counts[0]
    head = ["Dataset", "Events", "Partitions"] + [f"{w} worker{'s' if w != 1 else ''} (s)" for w in worker_counts]
    if len(worker_counts) > 1:
        head.append(f"Speed-up ({base_w}->{worker_counts[-1]})")
    head += [f"Throughput @{worker_counts[-1]}w (events/s)"]
    lines += ["## Processing time (M1), full analysis job", "", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for s in sizes:
        row = [s, f"{SIZES[s]:,}", f"{by[(s, base_w)][0]['partitions']:,}"]
        med = {}
        for w in worker_counts:
            vals = [r["seconds"] for r in by.get((s, w), [])]
            med[w] = statistics.median(vals) if vals else None
            row.append(f"{med[w]:.1f}" if vals else "-")
        if len(worker_counts) > 1:
            row.append(f"{med[base_w] / med[worker_counts[-1]]:.2f}x" if med[base_w] and med[worker_counts[-1]] else "-")
        last = by.get((s, worker_counts[-1]), [])
        row.append(f"{statistics.median(r['events_per_second'] for r in last):,.0f}" if last else "-")
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "## Query latency vs raw data volume (GET /analytics/admissions, 7 days x 8 depts)", "",
              "| Dataset | Raw events | p50 (ms) | p95 (ms) |", "|---|---|---|---|"]
    for s in sizes:
        if s in latency:
            lines.append(f"| {s} | {SIZES[s]:,} | {latency[s]['p50_ms']} | {latency[s]['p95_ms']} |")
    lines += ["", "Latency is client-side over HTTP on localhost with the cache bypassed. It reads "
              "pre-aggregated tables, so it depends on the requested range, not on raw volume."]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", nargs="+", default=list(SIZES), choices=list(SIZES))
    ap.add_argument("--workers", nargs="+", type=int, default=[1, 4])
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    results, latency = [], {}
    for size in args.sizes:
        load_dataset(size)
        token = h.login(h.ANALYST)
        for w in args.workers:
            h.say(f"\n-- {size} with {w} worker container(s) --")
            h.scale_workers(w)
            for i in range(args.repeat):
                r = run_once(token, size, w)
                h.say(f"   => {r['seconds']}s  ({r['events_per_second']:,} events/s)")
                results.append(r)
        start, end = data_range(size)
        d0 = date.fromisoformat(start) + timedelta(days=60)
        path = f"/api/v1/analytics/admissions?start_date={d0}&end_date={d0 + timedelta(days=6)}"
        latency[size] = h.measure_latency(token, path)
        h.say(f"   query latency @ {size}: p50 {latency[size]['p50_ms']} ms, p95 {latency[size]['p95_ms']} ms")

    env = environment()
    out_dir = h.ROOT / "benchmarks"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps({"environment": env, "runs": results, "query_latency": latency}, indent=2))
    (out_dir / "results.md").write_text(render_markdown(results, latency, env))
    h.say(f"\nwrote {out_dir / 'results.md'}\n")
    h.say((out_dir / "results.md").read_text())


if __name__ == "__main__":
    main()
