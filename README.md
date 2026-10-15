# Large-Scale Hospital Event Analytics (P35)

A cloud-deployable **batch analytics engine** that turns a large stream of synthetic hospital events into three
analytical products: **admission patterns**, **resource utilisation**, and **unusual workload periods**.
It is a data-intensive pipeline, not a CRUD app: a job request returns `202 Accepted` in milliseconds while a
pool of workers runs a MapReduce-style computation behind a broker.

```
        clients                    API tier (stateless)             processing tier                storage tier
 ┌──────────────────┐        ┌─────────────────────────┐      ┌───────────────────────┐     ┌──────────────────────┐
 │ analysts  (JWT)  │──────▶ │ FastAPI  (13+ endpoints)│ ───▶ │ Redis broker (queue)  │     │ Postgres  raw.*      │
 │ event source     │──────▶ │  auth · RBAC · rate-lim │      │   ▼                   │ ──▶ │ Postgres  analytics.*│
 │  (API key)       │        │  correlation IDs        │      │ Celery workers  1..N  │     │ Redis  shuffle/cache │
 │ load generator   │        └────────────┬────────────┘      │  map → shuffle →reduce│     │ Object storage (S3)  │
 └──────────────────┘                     └── reads results ───────────────────────────────────▶ analytics.* + cache
```

No caller ever reaches a worker directly. Everything crosses the broker, which is what lets the request path and the
processing path scale and fail independently. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the diagram and
the design reasoning.

## Quick start

Requires Docker Desktop. Nothing else.

```bash
docker compose up -d --build            # api, worker, postgres, redis, object storage (+ migrations and seeding)
open http://localhost:8000/             # the web dashboard (plain-language, for non-technical users)
open http://localhost:8000/docs         # Swagger UI (for developers)
```

Seeded **development** accounts (from `.env.example`; override in `.env`, never used outside `ENVIRONMENT=dev`):

| Who | Credential | Can do |
|---|---|---|
| `viewer@hospital.local` / `Viewer#12345` | JWT | read analytics and raw events |
| `analyst@hospital.local` / `Analyst#12345` | JWT | + submit / cancel / re-run jobs |
| `admin@hospital.local` / `Admin#12345` | JWT | + metrics, user and API-key management |
| ingest key `hea_dev_ingest_key_change_me` | `X-API-Key` header | ingest events (machines, not people) |

### Generate data and run an analysis

```bash
# 10k / 100k / 1M events (deterministic: same seed => identical data). ~8 s for the 1M set.
docker compose --profile tools run --rm tools python -m scripts.generate --events 10000   --start 2025-03-01 --end 2025-03-31 --seed 42 --out data/small.csv  --anomalies data/small_truth.json
docker compose --profile tools run --rm tools python -m scripts.generate --events 100000  --start 2025-01-01 --end 2025-06-30 --seed 42 --out data/medium.csv --anomalies data/medium_truth.json
docker compose --profile tools run --rm tools python -m scripts.generate --events 1000000 --start 2023-01-01 --end 2025-12-31 --seed 42 --out data/large.csv  --anomalies data/large_truth.json

# Bulk-load with Postgres COPY (~30k-80k rows/s). --truncate clears raw events and all results first.
docker compose --profile tools run --rm tools python -m scripts.load_csv data/small.csv --truncate

# The deployed path instead: upload to object storage, let a worker COPY it in via the API.
docker compose --profile tools run --rm tools python -m scripts.load_csv data/small.csv --via-storage
```

```bash
TOKEN=$(curl -s localhost:8000/api/v1/auth/login -H 'content-type: application/json' \
        -d '{"email":"analyst@hospital.local","password":"Analyst#12345"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 202 Accepted: the server has taken responsibility for the work but not done it.
curl -s localhost:8000/api/v1/jobs -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
     -d '{"job_type":"full_analysis","start_date":"2025-03-01","end_date":"2025-03-31"}'
# -> {"job_id":"...","status":"queued","partitions_total":248,"poll_url":"/api/v1/jobs/..."}

curl -s localhost:8000/api/v1/jobs/<job_id> -H "authorization: Bearer $TOKEN"        # watch partitions_completed climb
curl -s "localhost:8000/api/v1/analytics/anomalies?severity=critical" -H "authorization: Bearer $TOKEN"
curl -s "localhost:8000/api/v1/analytics/admissions?department_id=1&start_date=2025-03-01&end_date=2025-03-07" -H "authorization: Bearer $TOKEN"
```

Scale the processing tier by changing nothing but a replica count:

```bash
docker compose up -d --scale worker=4 --no-recreate worker
```

## The web interface

`http://localhost:8000/` is a light, minimal dashboard for hospital administrators. It uses open layouts, thin dividers and
one blue accent instead of boxed cards, and plain wording ("Bed utilisation", "Unusual workloads"). Seven pages share one
shell:

| Page | What it shows |
|---|---|
| Overview | key figures with change vs the previous period, admissions trend, departments compared, bed utilisation, recent unusual workloads and analyses |
| Admission patterns | trend with an optional previous-period overlay, busiest weekdays, admission sources, a sortable department table |
| Resource utilisation | per-department status (healthy / watch / constrained), the daily rhythm, a day-by-hour heatmap of where beds were tight |
| Unusual workloads | a filterable list by severity, measure and direction; each row expands to its detail |
| Analytics jobs | job history with live progress, details and concise errors, cancel, run again, and a guided "new analysis" form |
| Data ingestion | three steps: connect with an API key, send a single event or register a bulk file, check what arrived; admins manage keys |
| Reports | CSV exports of the existing data and a printable period summary |

It is plain HTML/CSS/JS with no build step and no external requests (hand-drawn SVG charts), served by the API itself from
`app/web/`. It is responsive down to phone width, keyboard accessible, and role-aware (viewers read, analysts also run
analyses, admins also see system health and manage keys). It only calls existing endpoints: the `/overview/*` summaries and
the analytics, jobs, events and admin APIs. The API key typed on the ingestion page is kept in memory only.

## What the system does

### Three planes

| Plane | Components | Optimised for | Scales by |
|---|---|---|---|
| **Ingest** | `POST /events`, `POST /events/batch` → `raw.hospital_events` | write throughput; never blocks on analytics | more API replicas |
| **Process** | Redis broker, Celery workers, chord reducer | parallel CPU over large scans | more worker replicas |
| **Query** | `GET /analytics/*` over `analytics.*` + Redis cache | low, flat read latency | cache, read replicas |

A heavy 1M-event job has no effect on ingest latency: the two paths share only Postgres and touch different tables.

### The pipeline (`app/pipeline/`)

1. **Partition** (`partition.py`): a job is split into one task per **(day × department)** slice, which gives slices of
   roughly equal size and so avoids stragglers. March across 3 departments is 93 partitions.
2. **Map** (`map_tasks.py`): each task streams its slice through a server-side cursor (flat memory), folds rows into a
   partial accumulator (counts, severity and length-of-stay sums, hour histogram, source mix, per-hour bed deltas) and
   writes nothing to the result tables. Pure and stateless ⇒ safely retryable.
3. **Shuffle** (`shuffle.py`): accumulators go to Redis under `job:{id}:key:{date}:{dept}`; the done-set is idempotent.
4. **Reduce** (`reduce.py`): a Celery **chord** fires the reducer only after every map task has completed. It computes
   daily admission figures, **hourly occupancy** (running sum of bed deltas over the department's opening occupancy)
   and **anomaly windows**.
5. **Persist**: `COPY` into a staging table then one `INSERT … ON CONFLICT DO UPDATE`, all in a single transaction, so a
   job writes everything or nothing and re-running overwrites instead of duplicating. Every result row carries the
   `job_id` that produced it.

| Job type | Writes |
|---|---|
| `admission_patterns` | `analytics.admission_metrics` (per date × department) |
| `resource_utilisation` | `analytics.resource_metrics` (per date × **hour** × department) |
| `anomaly_detection` | `analytics.anomaly_windows` |
| `full_analysis` | all three |

### Anomaly detection (`app/pipeline/anomaly.py`)

Flags unusual workload in **both directions**: an equipment outage that empties surgery is as reportable as a surge.
Each point is compared with a **seasonal baseline** (same hour and day-type for admissions; same hour and day-of-week
for occupancy), so daily and weekly cycles are *normal*, not alarms. Flagged values are kept out of the baseline so an
anomaly cannot teach the detector that it is normal, except a genuine sustained level shift, which is eventually
accepted. Occupancy uses a pooled noise estimate because a day-of-week × hour slot has too few samples for its own
standard deviation. Output: window, observed vs expected value, signed z-score, severity (`moderate` / `high` /
`critical`; occupancy at/above 95% is always critical).

The detector is **scored against ground truth**, not judged by eye: the generator injects known anomalies and records
them in `data/*_truth.json`.

```bash
docker compose --profile tools run --rm tools python -m scripts.score_anomalies data/large_truth.json --verbose
```

### REST API

Interactive docs at `/docs` (OpenAPI generated from the code). Everything is under `/api/v1/`.

| # | Method | Path | Auth | Purpose |
|---|---|---|---|---|
| 1 | POST | `/auth/register` | public | create an account (viewer; an admin grants more) |
| 2 | POST | `/auth/login` | public | access (15 min) + refresh (7 day) JWT pair |
| 3 | POST | `/auth/refresh` | refresh token | rotate; **reuse of a spent token revokes the whole session** |
| 4 | POST | `/events` | API key | ingest one event → `201` |
| 5 | POST | `/events/batch` | API key | bulk-load a CSV from object storage → `202` (+ `GET /events/batch/{id}`) |
| 6 | GET | `/events` | any role | raw events, filters, cursor pagination |
| 7 | POST | `/jobs` | analyst+ | submit an analytics job → **`202`** |
| 8 | GET | `/jobs/{id}` | analyst+ | status, progress, duration |
| 9 | GET | `/jobs` | analyst+ | list, filter by status (+ `POST /jobs/{id}/cancel`, `/rerun`) |
| 10 | GET | `/analytics/admissions` | any role | admission patterns |
| 11 | GET | `/analytics/resources` | any role | occupancy and staffing, hourly |
| 12 | GET | `/analytics/anomalies` | any role | unusual workload windows (severity / metric / direction filters) |
| 13 | GET | `/metrics` | admin | job time, ingest throughput, p50/p95/p99 latency, cache hit ratio |
| – | `GET/PATCH` `POST/DELETE` | `/admin/users`, `/admin/api-keys` | admin | roles; mint and revoke ingest keys |
| – | GET | `/health` | public | platform health check |

Conventions: cursor (keyset) pagination, never `OFFSET`; one error envelope
`{"error": {"code", "message", "correlation_id"}}`; `201/202/400/401/403/404/409/422/429/503` used for what they mean
(`400` malformed, `422` well-formed but unacceptable, `503` for an unreachable broker or database).

### Authentication and authorization

* **Humans**: bcrypt (cost 12) passwords, HS256 JWTs, refresh rotation with reuse detection (tokens stored hashed in Redis).
* **Machines**: random API keys, shown once, stored only as SHA-256; independently revocable; Redis token-bucket rate limit.
* **RBAC** is a FastAPI dependency (`Depends(require_role(Role.ANALYST))`), so a new endpoint cannot forget it.

| Capability | viewer | analyst | admin | API key |
|---|---|---|---|---|
| Read analytics / raw events | ✅ | ✅ | ✅ | |
| Submit a job | | ✅ | ✅ | |
| Cancel / re-run | | own jobs | any | |
| Ingest events / bulk load | | | | ✅ |
| Metrics, users, API keys | | | ✅ | |

### Logging, metrics

* **Structured JSON logs** on stdout (`structlog`), one object per line. A **correlation ID** is minted at the API edge
  (or taken from `X-Correlation-ID`), stored on the event row, passed through the broker into every task. One ID spans
  API → database → broker → worker. Passwords, keys, JWTs and request bodies are never logged.
* **Metrics** (`GET /metrics`): **M1** job processing time and events/s, **M2** ingestion throughput (single and bulk),
  **M3** per-endpoint p50/p95/p99, **M4** cache hit ratio.

## Failure tolerance

> pure, retryable map tasks over immutable input + idempotent result writes + late acknowledgement ⇒ any worker can die
> at any point and the job still produces the correct answer, only slower.

| Scenario | Behaviour | Demo |
|---|---|---|
| **Worker killed mid-job** | `acks_late` ⇒ the dead worker's tasks were never acknowledged; after the visibility timeout the broker redelivers them to survivors; result is byte-identical to a no-failure run | `python scripts/demo.py failure` |
| **Database down mid-job** | tasks retry with exponential backoff (1, 2, 4, 8, 16 s); a short outage is absorbed, a long one ends the job `partial` with a structured `error_detail` naming the failed partitions; the API answers `503` | `python scripts/demo.py db --down-seconds 15` / `90` |
| **Broker (Redis) down** | job submission returns `503` with `Retry-After` instead of hanging; **ingestion keeps working** because the write path never touches the broker | `python scripts/demo.py broker` |

Two hidden delays would make a worker-kill demo look like a hang, and both are handled (details in
[docs/PERFORMANCE.md](docs/PERFORMANCE.md)):

* Celery's Redis broker defaults to a **one-hour** visibility timeout. It is set to 45 s here (`CELERY_VISIBILITY_TIMEOUT`;
  it must exceed the longest task).
* kombu checks for dead workers' tasks every 10 s but only *acts* on every 10th check, adding up to ~100 s. `celery_app.py`
  makes it act on every check (safe: all tasks are idempotent). Measured recovery is now ~46 s after the kill.

`demo.py failure` holds each map task for 20 ms (`--slow-ms`) so the killed worker is certain to be mid-task; without it
the 5 ms tasks often finish before the kill lands and there is nothing to recover.

## Testing, benchmarks, load, demos

```bash
docker compose --profile tools run --rm tools python -m pytest        # 88 tests; uses a throwaway DB + Redis DBs 10-13

python scripts/benchmark.py --sizes small medium large --workers 1 4   # M1 across 10k/100k/1M  -> benchmarks/results.md
python scripts/benchmark.py --sizes large --workers 1 2 4              # the scalability experiment
python scripts/loadtest.py                                             # Locust: 10 / 100 / 500 / 1000 users -> benchmarks/*.html

python scripts/demo.py rbac | trace | failure | broker | db           # scripted evaluation demos
```

The `scripts/` host tools use only the Python standard library and talk to the stack through its REST API and the
`docker compose` CLI, exactly as an evaluator would. See [docs/DEMO_RUNBOOK.md](docs/DEMO_RUNBOOK.md) for the 12-minute
demonstration plan.

> Locust ingests live "now" events while it runs. Reload the dataset (`scripts.load_csv --truncate`) before benchmarking
> or demonstrating afterwards (`benchmark.py` does this itself).

### Measured results (one machine, 16 CPUs shared by everything; see [docs/PERFORMANCE.md](docs/PERFORMANCE.md))

| | 1 worker | 2 workers | 4 workers |
|---|---|---|---|
| Full analysis of **1,000,000 events** (8,768 partitions) | 43.4 s | 28.6 s | 23.7 s |
| 100,000 events | 7.1 s | 4.9 s | 3.7 s |
| 10,000 events | 1.2 s | 0.9 s | 0.7 s |

* Query latency is **flat** (p50 4-5 ms) while the raw data underneath grows 100×.
* Load (5 min/level): 10 users 8 rps · 100 users 79 rps · **500 users 387 rps, p95 28 ms, 0 errors** · 1,000 users 638 rps,
  p95 510 ms, only `429`s from the ingest rate limit.
* Worker killed at 40% of a 1M-event job: recovered in ~46 s, results **checksum-identical** to the no-failure run.
* Anomaly detection vs injected ground truth: **100% recall** (7/7) on the 100k and 1M datasets. Precision is honest, not
  flattering: of 69 windows reported on the 1M set, 38 are high/critical and 58% of those match an injected incident (the
  rest are chance single-hour spikes in small departments).

## Repository layout

```
app/
  api/v1/        routes: auth, events, jobs, analytics, metrics, admin
  core/          config, security, logging, deps (RBAC), middleware, errors, redis, metrics
  models/        SQLAlchemy models (raw / analytics / auth)
  schemas/       Pydantic request/response schemas
  pipeline/      partition.py, map_tasks.py, shuffle.py, reduce.py, anomaly.py, tasks.py, celery_app.py
  services/      jobs, ingest (COPY loader), cache, storage
migrations/      Alembic (raw schema partitioned by month, append-only trigger, result tables)
scripts/         generate.py, load_csv.py, score_anomalies.py, bootstrap.py | host: benchmark.py, loadtest.py, demo.py
loadtests/       locustfile.py
tests/           unit (anomaly, partition, map/reduce, generator) + integration (API, RBAC, jobs, failure paths)
  web/           the web interface: plain HTML/CSS/JS (css/, js/, js/pages/), no build step, served at /
docs/            ARCHITECTURE.md, PERFORMANCE.md, DEMO_RUNBOOK.md
docker-compose.yml  Dockerfile  .env.example
```

## Data model

Two schemas in one PostgreSQL 16 database.

* **`raw.*`**: immutable source of truth. `hospital_events` is **partitioned by month**, indexed for the
  (department, day) map scan, and **append-only enforced by a trigger** (`UPDATE`/`DELETE` raise). If every result table
  were dropped, all analytics could be regenerated from it. Also `departments`, `beds`, `ingest_batches`.
* **`analytics.*`**: derived, rewritable, small, read-hot: `admission_metrics`, `resource_metrics`, `anomaly_windows`,
  and the `analysis_jobs` ledger. Composite primary keys make reducer writes idempotent.
* **`auth.*`**: `users`, `api_keys` (hash only).

## Configuration

Twelve-factor: all settings come from environment variables, read once into a Pydantic `Settings` object. Defaults
work for local Compose. See [.env.example](.env.example). Notable: `JWT_SECRET` (the app refuses to start outside dev
with the default), `WORKER_CONCURRENCY`, `CELERY_VISIBILITY_TIMEOUT`, `DB_POOL_SIZE`, `S3_*`.

## Deviations from the design document

Things found while building that differ from, or extend, the written design. Each is deliberate.

| Design document | What was built, and why |
|---|---|
| Partition counts 31 / 186 / 1,095 for the three sizes | Those are *day* counts. With day × department partitioning (8 departments) they are **248 / 1,448 / 8,768** (the 3-year set has 1,096 days; one extra day from the leap year). |
| Eight tables | Plus `auth.users` and `auth.api_keys`, which authentication needs. |
| 13 endpoints | 13 as specified, plus job cancel/re-run, batch status, and admin user/key management, which the RBAC table requires. |
| MinIO for object storage | **SeaweedFS** (S3-compatible, `chrislusf/seaweedfs`): MinIO no longer publishes official images to public registries. The code uses plain `boto3`, so any S3 works. |
| "Faker + NumPy" generator | NumPy only. Patients are opaque `p_<n>` references, so Faker added nothing. |
| Anomaly detector: rolling 7-day mean/std per department | Seasonal slot baselines + flagged-value exclusion + pooled noise, found necessary by scoring against ground truth (a plain rolling mean flags every daily peak). |
| Weekend staffing gap: "staff-to-patient ratio doubles" | Staffing is a constant per department, so it is modelled as arrivals tripling over a weekend (patients per staff roughly doubles). |
| Occupancy from `length_of_stay_hours` | From per-hour bed **deltas** (arrival `+1`, departure `-1`) on top of an opening occupancy, which is robust across partition boundaries. The first 21 days of a dataset are excluded from the occupancy baseline (cold-start ramp from empty beds). |
| Visibility timeout (unspecified) | Set to 45 s; the Celery/Redis default of 1 h would hang the failure demo. |
| Single API container locally | Compose runs one; scale replicas with `--scale api=N` behind your own proxy, or on the cloud platform. |

## Out of scope

This is not a hospital management system: no patient portal, scheduling, billing, or real patient data. Patients exist
only as opaque identifiers attached to events.
