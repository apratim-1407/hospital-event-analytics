# Performance, scalability and what the measurements found

All numbers below were measured on one machine (Windows 11, Docker Desktop, 16 CPUs and 7.4 GiB visible to Docker),
everything (Postgres, Redis, API, workers and the load generator) sharing that hardware. Reproduce with the scripts in
`scripts/`; raw outputs are in [`../benchmarks/`](../benchmarks). **Re-run them on the evaluation machine and replace
these figures**: they describe the system's shape, not a platform guarantee.

## 1. Processing time across dataset sizes (metric M1)

Same `full_analysis` job (admission patterns + hourly resource utilisation + anomaly detection) over each dataset.
Median of 2 runs. Each worker container runs 2 processes.

| Dataset | Events | Partitions | 1 worker | 2 workers | 4 workers | Speed-up (1→4) | Throughput @4w |
|---|---|---|---|---|---|---|---|
| small | 10,000 | 248 | 1.2 s | 0.9 s | 0.7 s | 1.6× | 13.8k events/s |
| medium | 100,000 | 1,448 | 7.1 s | 4.9 s | 3.7 s | 1.9× | 26.8k events/s |
| large | 1,000,000 | 8,768 | 43.4 s | 28.6 s | 23.7 s | 1.8× | 42.2k events/s |

* **Processing time scales roughly linearly with event count** (10× the data → 5-6× the time at one worker, because
  fixed per-job costs matter at the small end).
* **Four workers do not give 4×.** Amdahl's law, measured: with 4 workers the map phase is ~2.7× faster, but the reduce
  stage is serial (≈ 8 s at 1M events: reading accumulators, hourly occupancy and anomaly detection for 8 departments,
  then writing 210k hourly rows), and the chord barrier means the job is only as fast as its slowest partition.
  Predicting this and then measuring it is the result.
* Reduce persistence was the biggest serial cost. Row-by-row `execute_values` upserts took **14 s**; staging with
  `COPY` and then one in-server `INSERT … ON CONFLICT DO UPDATE` takes **~7 s**; dropping a redundant secondary index
  on the 210k-row table shaved a little more.

## 2. Query latency is flat as raw data grows 100×

`GET /api/v1/analytics/admissions` (7 days × 8 departments), cache bypassed, client-side over localhost:

| Raw events underneath | p50 | p95 |
|---|---|---|
| 10,000 | 4.0 ms | 25.8 ms |
| 100,000 | 4.7 ms | 25.8 ms |
| 1,000,000 | 4.8 ms | 25.3 ms |

The endpoint reads pre-aggregated tables whose size depends on the date range and department count, not on how many raw
events exist. That flat line is the clearest single piece of evidence that the architecture does what it claims.

## 3. Load testing (Locust, 5 minutes per level)

Weighted mix: 50% read analytics, 25% ingest one event, 15% poll job status, 7% query raw events, 3% submit a job.
Run against the 4-process API (`API_WORKERS=4`) on the same machine as the load generator.

| Level | Users | Requests | Sustained RPS | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|---|
| Low | 10 | 2,376 | 7.9 | 5 ms | 9 ms | 75 ms | 0% |
| Medium | 100 | 23,593 | 78.9 | 5 ms | 9 ms | 25 ms | 0% |
| High | 500 | 115,977 | 387 | 7 ms | 28 ms | 68 ms | 0% |
| Breaking | 1,000 | 191,179 | 638 | 200 ms | 510 ms | 1.5 s | 6.4% |

The 6.4% at 1,000 users is **100% `429 rate limited`**: the single ingest API key is capped at 100 req/s by the token
bucket, and 1,000 users offer ~150 req/s of ingest. No `5xx`, no pool exhaustion. Reports:
`benchmarks/locust_{low,medium,high,breaking}.html`.

Beyond that (3,000 users, 2 min) the system is saturated and honest about it: the API container sits at ~380% CPU (all 4
processes), Postgres at ~350%, and the single-process Locust generator itself at 100% of a core; latencies climb to
seconds and ~3% of requests are shed as `503` + `Retry-After`. The first thing to give is **bcrypt**: 3,000 simultaneous
new sessions at cost factor 12 is ~750 CPU-seconds of password hashing, which is why login p50 reaches tens of seconds
while already-authenticated traffic keeps flowing.

### What the load test found and how it was fixed

The first load run was bad, and diagnosing it was the useful part. The design predicted the bottleneck would be the
database connection pool; it was, but not for the reason a simple "make the pool bigger" would fix.

| | 500 users | 1,000 users |
|---|---|---|
| **First run** | 28.6 rps, p95 **46 s**, 14% errors | 4.8 rps, 100% errors |
| After fixing the amplifiers (below) | 384 rps, p95 82 ms, 0% | 27.8 rps, p95 90 s, 46% errors under *sustained* overload |
| After releasing connections early + serving cached JSON + 4 API processes | **387 rps, p95 28 ms, 0%** | **638 rps, p95 510 ms**, only `429`s |

Root causes, in the order they were found (all visible in the logs as `QueuePool limit of size 20 overflow 10 reached`):

1. **A sync generator dependency shared the 40-thread pool with the endpoints.** Under load every thread parked waiting
   for a pooled connection, while the teardown that would *release* one needed a free thread. Deadlock until the pool
   timeout. Fix: `get_db` is an `async` generator (a `Session` opens no connection until its first query, and
   `close()` just returns it), and the thread limit is raised above the pool size.
2. **Login held a pooled connection through a ~250 ms bcrypt.** 500 simultaneous logins pinned the whole pool. Fix: read
   the user row, return the connection, then hash.
3. **Per-request Redis metric writes ran synchronously on the event loop.** Fix: in-memory buffers flushed once a second.
4. **A pooled connection was held until request teardown, which runs on the (saturated) event loop**, so connection
   hold time included event-loop queueing. Fix: release the connection in the handler thread the moment the queries
   finish.
5. **Cached analytics responses were parsed, Pydantic-validated and re-serialised on every hit.** Fix: cache and return
   the already-serialised JSON string.
6. **One API process = one core.** Fix: 4 uvicorn workers per container (`API_WORKERS`).
7. Pool exhaustion now surfaces as `503` + `Retry-After` (honest backpressure) instead of an anonymous `500`.

## 4. Failure recovery cost

`docker kill hospital-worker-2` at 40% of a 1M-event job on 4 workers. Last recorded run (see
`benchmarks/failure_evidence/`): the job completed with **checksums over all 219,000+ result rows identical to the
no-failure run**; the two tasks the dead worker was holding were redelivered to surviving workers ~46 s after the kill.

Two non-obvious things this exposed:

* Celery's Redis broker defaults to a **one-hour** visibility timeout, so a dead worker's work would sit for an hour.
  Set to 45 s (`CELERY_VISIBILITY_TIMEOUT`); it must exceed the longest task, so a long reduce is the thing to watch
  when data grows.
* kombu runs its recovery check every 10 s but **acts on only every 10th check**, so recovery took the visibility timeout
  *plus up to ~100 s* (measured: 122 s for a 45 s timeout). `celery_app.py` makes it act on every check.
* A kill can also land when the victim's tasks have finished but not yet been acknowledged; then nothing is lost and
  the dead worker's tasks are redelivered after the job is already done (a harmless duplicate). `demo.py failure
  --slow-ms` holds each task briefly so the demonstration is deterministic.

## 5. Where the time goes, and what would be next

* **Map phase**: ~3.4 ms of CPU per task, dominated by Celery's per-task overhead (message, result backend, chord
  counter), not by SQL. Coarser partitions (a week × department) would cut it, at the cost of less even slices.
* **Reduce phase**: serial. Parallelising it by department (each department's occupancy series is independent) is the
  most valuable next optimisation and the way past Amdahl.
* **Postgres** is the next shared bottleneck under heavy ingest + read: PgBouncer in transaction mode and a read
  replica for the query tier.
* **Auth**: a hot path is bcrypt. Fine for interactive logins; machine clients already avoid it by using API keys.

## Caveats

* Load generator, API, workers and databases compete for the same CPUs, so the "breaking point" is a property of this
  machine. The Oracle VM (2 OCPU) will be lower; the *shape* (what saturates first, and that it sheds load as `503`/`429`
  rather than collapsing) is what carries over.
* Locust ingests live "now" events during a run. Reload the dataset (`scripts.load_csv --truncate`) before benchmarking
  or demonstrating afterwards; `benchmark.py` does this itself.
