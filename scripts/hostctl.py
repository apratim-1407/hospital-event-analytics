"""Host-side helpers for benchmark.py and demo.py.

These run on the host with plain Python (standard library only): they drive the
stack through its public REST API and the `docker compose` CLI, exactly as an
evaluator would.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = os.environ.get("API_BASE_URL", "http://localhost:8000")
ANALYST = (os.environ.get("SEED_ANALYST_EMAIL", "analyst@hospital.local"), os.environ.get("SEED_ANALYST_PASSWORD", "Analyst#12345"))
ADMIN = (os.environ.get("SEED_ADMIN_EMAIL", "admin@hospital.local"), os.environ.get("SEED_ADMIN_PASSWORD", "Admin#12345"))
VIEWER = (os.environ.get("SEED_VIEWER_EMAIL", "viewer@hospital.local"), os.environ.get("SEED_VIEWER_PASSWORD", "Viewer#12345"))
API_KEY = os.environ.get("SEED_INGEST_API_KEY", "hea_dev_ingest_key_change_me")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def http(method: str, path: str, body=None, token: str | None = None, headers: dict | None = None, timeout: float = 60):
    """Returns (status, parsed_json_or_text, response_headers). Never raises on HTTP errors."""
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            status, hdrs = resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read().decode(), e.code, dict(e.headers)
    try:
        return status, json.loads(raw), {k.lower(): v for k, v in hdrs.items()}
    except json.JSONDecodeError:
        return status, raw, {k.lower(): v for k, v in hdrs.items()}


def login(creds) -> str:
    status, body, _ = http("POST", "/api/v1/auth/login", {"email": creds[0], "password": creds[1]})
    if status != 200:
        sys.exit(f"login failed for {creds[0]}: {status} {body}")
    return body["access_token"]


def compose(*args: str, capture: bool = True, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, capture_output=capture, text=True, encoding="utf-8", errors="replace",
        check=check, env={**os.environ, **env} if env else None,
    )


def psql(sql: str, dbname: str = "hospital") -> str:
    out = compose("exec", "-T", "postgres", "psql", "-U", "hospital", "-d", dbname, "-At", "-c", sql)
    return out.stdout.strip()


def scale_workers(n: int, task_sleep_ms: int | None = None) -> None:
    """Run n worker containers. With task_sleep_ms the containers are recreated with that
    MAP_TASK_SLEEP_MS (demo aid); otherwise existing ones are left alone."""
    if task_sleep_ms is not None:
        compose("up", "-d", "--scale", f"worker={n}", "--force-recreate", "--no-deps", "worker", env={"MAP_TASK_SLEEP_MS": str(task_sleep_ms)})
    else:
        compose("up", "-d", "--scale", f"worker={n}", "--no-recreate", "worker")
    deadline = time.time() + 60
    while time.time() < deadline:
        running = compose("ps", "--status", "running", "--format", "{{.Name}}", "worker").stdout.split()
        if len(running) == n:
            break
        time.sleep(1)
    time.sleep(5)  # let the new workers connect to the broker


def worker_containers() -> list[str]:
    return sorted(compose("ps", "--status", "running", "--format", "{{.Name}}", "worker").stdout.split())


def submit_job(token: str, job_type: str, start: str, end: str, **extra) -> dict:
    status, body, _ = http("POST", "/api/v1/jobs", {"job_type": job_type, "start_date": start, "end_date": end, **extra}, token)
    if status != 202:
        sys.exit(f"job submission failed: {status} {body}")
    return body


def get_job(token: str, job_id: str) -> dict:
    return http("GET", f"/api/v1/jobs/{job_id}", token=token)[1]


def wait_for_job(token: str, job_id: str, on_poll=None, timeout: float = 1800) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = get_job(token, job_id)
        if on_poll:
            on_poll(job)
        if job["status"] in ("completed", "failed", "partial", "cancelled"):
            return job
        time.sleep(0.5)
    sys.exit("timed out waiting for the job")


def percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    if not s:
        return float("nan")
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def measure_latency(token: str, path: str, n: int = 60) -> dict:
    """Client-side latency of one endpoint, with the cache bypassed by varying a no-op param."""
    samples = []
    for i in range(n):
        t0 = time.perf_counter()
        http("GET", f"{path}{'&' if '?' in path else '?'}limit={100 + (i % 7)}", token=token)
        samples.append((time.perf_counter() - t0) * 1000)
    return {"p50_ms": round(percentile(samples, 50), 2), "p95_ms": round(percentile(samples, 95), 2), "n": n}
