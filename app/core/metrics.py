"""Operational metrics, kept in Redis so every API replica contributes to one set of numbers.

M1 job processing time   -> analytics.analysis_jobs (started_at / finished_at)
M2 ingestion throughput  -> per-minute counters here + raw.ingest_batches for bulk loads
M3 API latency p50/p95/p99 -> bounded latency samples per endpoint
M4 cache hit ratio       -> hit / miss counters

Recording is buffered in memory and flushed to Redis once a second by a background thread.
Writing to Redis per request put a synchronous network round trip on the request path (and,
for the request-edge middleware, on the event loop itself), which under load cost more than
the requests it was measuring. Numbers are therefore at most ~1 s stale.
"""
import threading
import time
from collections import Counter, defaultdict

import numpy as np

from app.core.redis import guarded

LATENCY_SAMPLES = 5000
FLUSH_SECONDS = 1.0

_lock = threading.Lock()
_latencies: dict[str, list[float]] = defaultdict(list)
_status: Counter = Counter()
_counters: Counter = Counter()  # "m:cache:hits", "m:ingest:<minute>", ...
_flusher: threading.Thread | None = None


def record_request(endpoint: str, status: int, duration_ms: float) -> None:
    with _lock:
        _latencies[endpoint].append(round(duration_ms, 3))
        _status[str(status)] += 1


def record_cache(hit: bool) -> None:
    with _lock:
        _counters["m:cache:hits" if hit else "m:cache:misses"] += 1


def record_ingested(n: int = 1) -> None:
    with _lock:
        _counters[f"m:ingest:{int(time.time() // 60)}"] += n


def flush() -> None:
    """Push buffered measurements to Redis. Fail-open: if Redis is down they are dropped."""
    with _lock:
        lat, status, counters = dict(_latencies), dict(_status), dict(_counters)
        _latencies.clear()
        _status.clear()
        _counters.clear()
    if not (lat or status or counters):
        return

    def op(r):
        pipe = r.pipeline(transaction=False)
        for endpoint, samples in lat.items():
            key = f"m:lat:{endpoint}"
            pipe.lpush(key, *samples[-LATENCY_SAMPLES:])
            pipe.ltrim(key, 0, LATENCY_SAMPLES - 1)
        for code, n in status.items():
            pipe.hincrby("m:status", code, n)
        for key, n in counters.items():
            pipe.incrby(key, n)
            if key.startswith("m:ingest:"):
                pipe.expire(key, 3600)
        pipe.execute()

    guarded(op)


def _flush_loop() -> None:
    while True:
        time.sleep(FLUSH_SECONDS)
        try:
            flush()
        except Exception:
            pass


def start_flusher() -> None:
    global _flusher
    if _flusher is None or not _flusher.is_alive():
        _flusher = threading.Thread(target=_flush_loop, name="metrics-flusher", daemon=True)
        _flusher.start()


def ingest_rate(window_minutes: int = 5) -> dict:
    flush()
    now_min = int(time.time() // 60)

    def op(r):
        keys = [f"m:ingest:{now_min - i}" for i in range(window_minutes)]
        return sum(int(v or 0) for v in r.mget(keys))

    total = guarded(op, default=None)
    if total is None:
        return {"available": False}
    return {
        "available": True,
        "window_minutes": window_minutes,
        "events": total,
        "events_per_second": round(total / (window_minutes * 60), 3),
    }


def latency_summary() -> dict:
    flush()

    def op(r):
        out = {}
        for key in r.scan_iter(match="m:lat:*", count=200):
            samples = np.array([float(x) for x in r.lrange(key, 0, -1)])
            if samples.size == 0:
                continue
            out[key.removeprefix("m:lat:")] = {
                "count": int(samples.size),
                "p50": round(float(np.percentile(samples, 50)), 2),
                "p95": round(float(np.percentile(samples, 95)), 2),
                "p99": round(float(np.percentile(samples, 99)), 2),
                "max": round(float(samples.max()), 2),
            }
        return dict(sorted(out.items()))

    return guarded(op, default={}) or {}


def cache_summary() -> dict:
    flush()

    def op(r):
        hits, misses = r.mget(["m:cache:hits", "m:cache:misses"])
        return int(hits or 0), int(misses or 0)

    hits, misses = guarded(op, default=(0, 0))
    total = hits + misses
    return {"hits": hits, "misses": misses, "hit_ratio": round(hits / total, 4) if total else None}


def status_counts() -> dict:
    flush()
    return guarded(lambda r: r.hgetall("m:status"), default={}) or {}
