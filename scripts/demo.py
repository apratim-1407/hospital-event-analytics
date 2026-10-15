"""Scripted evaluation demos. Each one prints what it is proving and the evidence.

    python scripts/demo.py rbac       # 403 for the wrong role, 401 for JWT on ingest, hashed keys at rest
    python scripts/demo.py trace      # follow ONE event: API -> raw table -> map -> reduce -> result row
    python scripts/demo.py failure    # kill a worker mid-job; the survivors finish it, same answer
    python scripts/demo.py broker     # stop Redis: job submission 503s, ingestion keeps working
    python scripts/demo.py db         # stop Postgres mid-job: retry with backoff, then recover or fail cleanly

Needs the stack up with data loaded (docker compose up -d --build; scripts/benchmark.py or
scripts.load_csv). Standard library only; run with the host Python.
"""
import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime

import hostctl as h


def banner(title: str, claim: str) -> None:
    h.say("\n" + "=" * 78)
    h.say(f" {title}")
    h.say(f" proves: {claim}")
    h.say("=" * 78)


def data_window() -> tuple[str, str]:
    lo, hi = h.psql("SELECT (min(event_time) AT TIME ZONE 'UTC')::date, (max(event_time) AT TIME ZONE 'UTC')::date FROM raw.hospital_events").split("|")
    if not lo:
        sys.exit("no data loaded; run scripts/benchmark.py or scripts.load_csv first")
    return lo, hi


def sample_event(day: str, patient: str) -> dict:
    return {"patient_ref": patient, "event_type": "admission", "department_id": 1, "event_time": f"{day}T12:00:00Z",
            "severity": 4, "admission_source": "emergency", "bed_id": "E-001"}


def logs_for(cid: str, services=("api", "worker")) -> list[dict]:
    out = h.compose("logs", "--no-log-prefix", *services).stdout
    rows = []
    for line in out.splitlines():
        if cid in line and line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return sorted(rows, key=lambda r: r.get("ts", ""))


# ------------------------------------------------------------------------ rbac
def rbac(_):
    banner("Authentication and authorization", "RBAC is enforced centrally; ingest needs an API key, not a JWT")
    viewer, analyst, admin = h.login(h.VIEWER), h.login(h.ANALYST), h.login(h.ADMIN)
    job = {"job_type": "admission_patterns", "start_date": "2025-03-01", "end_date": "2025-03-02"}
    s, b, _ = h.http("POST", "/api/v1/jobs", job, viewer)
    h.say(f"\n1. viewer  POST /jobs            -> {s}  {b['error']['code']}: {b['error']['message']}")
    s, b, _ = h.http("POST", "/api/v1/jobs", job, analyst)
    h.say(f"2. analyst POST /jobs            -> {s}  job {b.get('job_id', '')[:8]} status={b.get('status')}")
    s, b, _ = h.http("POST", "/api/v1/events", sample_event("2025-03-01", "p_rbac"), admin)
    h.say(f"3. admin JWT on POST /events      -> {s}  {b['error']['message']}   (ingest needs X-API-Key)")
    s, b, _ = h.http("GET", "/api/v1/metrics", token=analyst)
    h.say(f"4. analyst GET /metrics           -> {s}  {b['error']['message']}")
    s, b, _ = h.http("GET", "/api/v1/metrics", token=admin)
    h.say(f"5. admin   GET /metrics           -> {s}  (keys: {', '.join(list(b)[1:4])} ...)")
    h.say("\n6. API keys at rest (hash only; the plaintext exists nowhere in the system):")
    h.say("   " + h.psql("SELECT key_id, name, key_prefix, key_hash FROM auth.api_keys ORDER BY key_id LIMIT 3").replace("\n", "\n   "))


# ----------------------------------------------------------------------- trace
def trace(_):
    banner("Trace one record through the pipeline", "a single correlation ID threads API -> database -> broker -> worker -> result")
    _, hi = data_window()
    cid, patient = str(uuid.uuid4()), f"p_trace_{uuid.uuid4().hex[:6]}"
    s, body, hdrs = h.http("POST", "/api/v1/events", sample_event(hi, patient), headers={"X-API-Key": h.API_KEY, "X-Correlation-ID": cid})
    h.say(f"\n1. POST /events with X-Correlation-ID {cid}\n   -> {s} {json.dumps(body)}")
    h.say("\n2. The row in raw.hospital_events:")
    h.say("   " + h.psql(f"SELECT event_id, patient_ref, event_type, event_time, ingested_at, correlation_id FROM raw.hospital_events WHERE correlation_id = '{cid}'"))

    analyst = h.login(h.ANALYST)
    accepted = h.submit_job(analyst, "admission_patterns", hi, hi, trace_correlation_ids=[cid])
    h.say(f"\n3. POST /jobs for that day (trace_correlation_ids=[...]) -> 202, {accepted['partitions_total']} partitions")
    job = h.wait_for_job(analyst, accepted["job_id"])
    h.say(f"   job {job['status']} in {job['duration_seconds']}s, {job['events_processed']:,} events processed")

    time.sleep(1)
    h.say(f"\n4. Every log line carrying {cid[:8]}... (API and workers):\n")
    for r in logs_for(cid):
        extra = {k: v for k, v in r.items() if k not in ("ts", "level", "service", "event", "correlation_id", "job_id")}
        h.say(f"   {r['ts'][11:23]}  {r['service'][:18]:<18} {r['event']:<30} {json.dumps(extra)}")

    h.say("\n5. The processed row, with its job_id lineage (separate schema from the raw data):")
    h.say("   " + h.psql(f"SELECT metric_date, department_id, admission_count, peak_hour, job_id FROM analytics.admission_metrics WHERE job_id = '{accepted['job_id']}' AND department_id = 1"))


# --------------------------------------------------------------------- failure
CHECKSUMS = {
    "admissions": "SELECT md5(string_agg(format('%s|%s|%s|%s|%s|%s|%s|%s|%s', metric_date, department_id, admission_count, discharge_count, transfer_count, avg_severity, avg_length_of_stay_hours, peak_hour, source_breakdown::text), ',' ORDER BY metric_date, department_id)), count(*) FROM analytics.admission_metrics",
    "resources": "SELECT md5(string_agg(format('%s|%s|%s|%s|%s|%s|%s|%s', metric_date, metric_hour, department_id, occupied_beds, bed_capacity, occupancy_rate, icu_occupancy_rate, staff_to_patient_ratio), ',' ORDER BY metric_date, metric_hour, department_id)), count(*) FROM analytics.resource_metrics",
    "anomalies": "SELECT md5(string_agg(format('%s|%s|%s|%s|%s|%s|%s|%s', window_start, window_end, department_id, metric_name, observed_value, expected_value, z_score, severity_label), ',' ORDER BY department_id, metric_name, window_start)), count(*) FROM analytics.anomaly_windows",
}


def result_checksums() -> dict:
    return {name: h.psql(sql) for name, sql in CHECKSUMS.items()}


def failure(args):
    banner("Controlled failure: kill a worker mid-job", "late acks + pure map tasks + idempotent writes => correct result, only slower")
    workers = h.worker_containers()
    if len(workers) < 2:
        sys.exit("need at least 2 workers: docker compose up -d --scale worker=4")
    victim = args.victim or workers[1]
    if args.slow_ms:
        h.say(f"\nholding each map task for {args.slow_ms} ms (demo aid) so the victim is certain to be mid-task when killed...")
        h.scale_workers(len(workers), task_sleep_ms=args.slow_ms)
        workers = h.worker_containers()
        victim = args.victim or workers[1]
    lo, hi = data_window()
    h.say(f"\nworkers: {', '.join(workers)}\ndata window: {lo} .. {hi}   victim: {victim}")
    analyst = h.login(h.ANALYST)

    h.say("\n-- baseline run (no failure) --")
    base = h.wait_for_job(analyst, h.submit_job(analyst, "full_analysis", lo, hi)["job_id"])
    base_sum = result_checksums()
    h.say(f"   {base['status']} in {base['duration_seconds']}s, {base['events_processed']:,} events")
    h.say("   result checksums: " + json.dumps({k: v.split('|')[0][:12] + ' rows=' + v.split('|')[1] for k, v in base_sum.items()}))

    h.say("\n-- clearing derived tables so the next run must recompute everything --")
    h.psql("TRUNCATE analytics.admission_metrics, analytics.resource_metrics, analytics.anomaly_windows")

    h.say(f"\n-- failure run: will `docker kill {victim}` at {args.kill_at}% --")
    accepted = h.submit_job(analyst, "full_analysis", lo, hi)
    killed = {"at": None, "pct": None, "last_print": 0.0}

    def watch(job):
        if killed["at"] is None and job["progress_pct"] >= args.kill_at:
            killed["at"], killed["pct"] = datetime.now(UTC), job["progress_pct"]
            subprocess.run(["docker", "kill", victim], capture_output=True, check=False)
            h.say(f"   !! {killed['at']:%H:%M:%S.%f}  docker kill {victim}  (job at {job['progress_pct']}%, {job['partitions_completed']:,}/{job['partitions_total']:,} partitions)")
        elif killed["at"] and time.time() - killed["last_print"] >= 5:
            killed["last_print"] = time.time()
            h.say(f"      {job['progress_pct']:5.1f}%  ({job['partitions_completed']:,}/{job['partitions_total']:,})")

    job = h.wait_for_job(analyst, accepted["job_id"], on_poll=watch)
    after_sum = result_checksums()
    h.say(f"\n   {job['status']} in {job['duration_seconds']}s (baseline {base['duration_seconds']}s), {job['events_processed']:,} events")

    time.sleep(1)
    survivors = [w for w in workers if w != victim]
    lines = []
    for svc_line in h.compose("logs", "--no-log-prefix", "worker").stdout.splitlines():
        if accepted["job_id"] in svc_line and ("map_task.redelivered" in svc_line or "map_task.retry" in svc_line or "worker.shutdown" in svc_line):
            lines.append(svc_line)
    # The broker redelivers only after the visibility timeout, which can fall after the job ends if the
    # dead worker's tasks had already finished (done but un-acked). Wait it out so the evidence is complete.
    wait_until = time.time() + (0 if killed["at"] is None else 50)
    def _redelivered():
        return [json.loads(x) for x in h.compose("logs", "--no-log-prefix", "worker").stdout.splitlines()
                if accepted["job_id"] in x and x.startswith("{") and "map_task.redelivered" in x]
    redelivered = _redelivered()
    while not redelivered and time.time() < wait_until:
        time.sleep(5)
        redelivered = _redelivered()
    h.say(f"\n   redelivered tasks logged by surviving workers: {len(redelivered)}")
    for r in redelivered[:6]:
        h.say(f"     {r['ts'][11:23]}  {r['service']:<22} {r['event']}  partition={r.get('partition')}")

    same = after_sum == base_sum
    h.say("\n   result checksums after failure: " + json.dumps({k: v.split('|')[0][:12] + ' rows=' + v.split('|')[1] for k, v in after_sum.items()}))
    h.say(f"\n   IDENTICAL TO THE NO-FAILURE RUN: {'YES' if same else 'NO  <-- correctness violated'}")
    h.say(f"   extra time caused by the failure: {job['duration_seconds'] - base['duration_seconds']:.1f}s "
          f"(the broker waits {h.compose('exec', '-T', 'api', 'printenv', 'CELERY_VISIBILITY_TIMEOUT').stdout.strip()}s before redelivering a dead worker's tasks)")

    ev_dir = h.ROOT / "benchmarks" / "failure_evidence" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    ev_dir.mkdir(parents=True, exist_ok=True)
    (ev_dir / "summary.json").write_text(json.dumps({
        "victim": victim, "survivors": survivors, "killed_at": killed["at"].isoformat() if killed["at"] else None,
        "progress_at_kill_pct": killed["pct"], "baseline": base, "failure_run": job,
        "checksums_baseline": base_sum, "checksums_after_failure": after_sum, "identical": same,
        "redelivered_tasks": len(redelivered),
    }, indent=2, default=str))
    (ev_dir / "worker_log_excerpt.jsonl").write_text("\n".join(lines))
    h.say(f"\n   evidence saved to {ev_dir.relative_to(h.ROOT)}")
    if not args.no_restore:
        h.say(f"\n-- restoring {len(workers)} workers at normal speed --")
        h.scale_workers(len(workers), task_sleep_ms=0)
    if not same:
        sys.exit(1)


# ---------------------------------------------------------------------- broker
def broker(_):
    banner("Broker unavailable", "analytics degrades honestly (503) while ingestion keeps capturing events")
    analyst = h.login(h.ANALYST)
    _, hi = data_window()
    h.say("\n-- stopping Redis --")
    h.compose("stop", "redis")
    try:
        time.sleep(2)
        s, b, hdrs = h.http("POST", "/api/v1/jobs", {"job_type": "admission_patterns", "start_date": hi, "end_date": hi}, analyst)
        h.say(f"1. POST /jobs   -> {s} {b['error']['code']}: {b['error']['message']}  (Retry-After: {hdrs.get('retry-after')})")
        cid = str(uuid.uuid4())
        s, b, _ = h.http("POST", "/api/v1/events", sample_event(hi, "p_during_broker_outage"), headers={"X-API-Key": h.API_KEY, "X-Correlation-ID": cid})
        h.say(f"2. POST /events -> {s}  event {b.get('event_id')} stored   <- ingestion unaffected: nothing is lost")
        s, b, _ = h.http("GET", "/health")
        h.say(f"3. GET /health  -> {s} {json.dumps(b)}   <- reported, not fatal")
        s, b, _ = h.http("GET", "/api/v1/analytics/admissions?limit=1", token=analyst)
        h.say(f"4. GET /analytics/admissions -> {s}   <- reads fall through the cache to Postgres")
    finally:
        h.say("\n-- starting Redis --")
        h.compose("start", "redis")
    time.sleep(4)
    s, b, _ = h.http("POST", "/api/v1/jobs", {"job_type": "admission_patterns", "start_date": hi, "end_date": hi}, h.login(h.ANALYST))
    h.say(f"5. after recovery: POST /jobs -> {s}  job {b.get('job_id', '')[:8]} accepted")


# -------------------------------------------------------------------------- db
def _db_down(mode: str) -> None:
    if mode == "stop":
        h.compose("stop", "postgres")
        return
    # Refuse new connections and drop existing ones: the database is "unreachable" and every
    # attempt fails at once. (`stop` behaves the same, but Docker Desktop's DNS makes each failed
    # attempt to a stopped container wait 5-40s before erroring, which only slows the demo.)
    h.psql("ALTER DATABASE hospital WITH ALLOW_CONNECTIONS false", "postgres")
    h.psql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'hospital' AND pid <> pg_backend_pid()", "postgres")


def _db_up(mode: str) -> None:
    if mode == "stop":
        h.compose("start", "postgres")
    else:
        h.psql("ALTER DATABASE hospital WITH ALLOW_CONNECTIONS true", "postgres")


def db(args):
    banner("Database connection loss mid-job", "transient failures retry with exponential backoff; permanent ones fail cleanly and say why")
    analyst = h.login(h.ANALYST)
    lo, hi = data_window()
    accepted = h.submit_job(analyst, "full_analysis", lo, hi)
    h.say(f"\njob {accepted['job_id'][:8]} submitted ({accepted['partitions_total']:,} partitions)")
    while h.get_job(analyst, accepted["job_id"])["progress_pct"] < 15:
        time.sleep(0.3)
    t_down = time.time()
    h.say(f"-- database unreachable for {args.down_seconds}s (retry window is ~31s: 1+2+4+8+16) --")
    _db_down(args.mode)
    s, b, _ = h.http("GET", "/api/v1/analytics/admissions?limit=1", token=analyst)
    h.say(f"   meanwhile the API answers {s}: {b['error']['message'] if isinstance(b, dict) and 'error' in b else b}")
    time.sleep(max(0, args.down_seconds - (time.time() - t_down)))
    h.say("-- database restored --")
    _db_up(args.mode)

    deadline = time.time() + 240
    job = None
    while time.time() < deadline:
        try:
            job = h.get_job(analyst, accepted["job_id"])
            if isinstance(job, dict) and job.get("status") in ("completed", "failed", "partial"):
                break
        except Exception:
            pass
        time.sleep(2)
    h.say(f"\nfinal state: {job['status'] if isinstance(job, dict) and 'status' in job else job}")
    if isinstance(job, dict) and job.get("error_detail"):
        h.say("error_detail: " + json.dumps(job["error_detail"])[:400])
    lines = [ln for ln in h.compose("logs", "--no-log-prefix", "worker").stdout.splitlines() if accepted["job_id"] in ln and ln.startswith("{")]
    events = {}
    for ln in lines:
        r = json.loads(ln)
        events.setdefault(r["event"], []).append(r)
    for name in ("map_task.retry", "map_task.failed_permanently", "reduce.retry", "reduce.failed_permanently"):
        if name in events:
            h.say(f"  {len(events[name]):>5} x {name}")
    attempts = sorted({r.get("retry") for r in events.get("map_task.retry", [])})
    if attempts:
        h.say(f"  backoff attempts seen: {attempts}  (countdowns: {sorted({r.get('countdown') for r in events['map_task.retry']})} seconds)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rbac").set_defaults(fn=rbac)
    sub.add_parser("trace").set_defaults(fn=trace)
    f = sub.add_parser("failure")
    f.add_argument("--victim", help="container to kill (default: the second worker)")
    f.add_argument("--kill-at", type=float, default=40.0, help="job progress %% at which to kill")
    f.add_argument("--no-restore", action="store_true")
    f.add_argument("--slow-ms", type=int, default=20, help="hold each map task this long (0 = real speed; the kill may then land between tasks)")
    f.set_defaults(fn=failure)
    sub.add_parser("broker").set_defaults(fn=broker)
    d = sub.add_parser("db")
    d.add_argument("--down-seconds", type=int, default=10, help="short = recovers within the retry window; 60 = permanent")
    d.add_argument("--mode", choices=["block", "stop"], default="block", help="block: refuse connections (default); stop: stop the container")
    d.set_defaults(fn=db)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
