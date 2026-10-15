# Demonstration runbook (about 12 minutes)

The mandatory demonstration covers: API execution, database and storage state, logs and metrics, the cloud
architecture, and a trace of one sample record through the pipeline.

## Before you start (once, ~3 minutes, do not do this live)

```bash
docker compose up -d --build --scale worker=4          # 4 workers: containers are called hospital-worker-1..4
docker compose --profile tools run --rm tools python -m scripts.load_csv data/large.csv --truncate    # 1M events
python scripts/benchmark.py --sizes large --workers 4  # runs a full job so result tables are populated
```

Arrange four panes:

1. a terminal at the repo root (for `python scripts/demo.py ...` and `curl`)
2. `docker compose exec postgres psql -U hospital -d hospital`
3. `docker compose logs -f worker`
4. a browser on `http://localhost:8000/docs`

Rehearse the whole thing, especially the worker kill. If the deployed environment misbehaves on the day, the local
Compose stack is the hot standby: it runs the identical topology.

## Run sheet

| Min | Do | Say / show | Proves |
|---|---|---|---|
| 0-1 | Open [ARCHITECTURE.md](ARCHITECTURE.md) diagram | four tiers; *no path from the API to a worker*; everything crosses the broker | cloud architecture |
| 1-2 | `python scripts/demo.py rbac` | viewer `403`, analyst `202`, JWT on ingest `401`, API key is a hash in the DB | auth and RBAC |
| 2-3 | in psql: `\dt raw.*` `\dt analytics.*`; `SELECT count(*) FROM raw.hospital_events;` | two schemas; raw is append-only (`UPDATE` raises); 1M rows | raw vs processed storage; dataset size |
| 3-7 | `python scripts/demo.py trace` | POST one event with a visible correlation ID → the row in psql → submit a job over that day → every log line carrying the ID: ingest, map task, reduce → the result row with its `job_id` | the mandatory single-record trace; asynchrony |
| 7-8 | in psql: `SELECT * FROM analytics.admission_metrics WHERE job_id = '<id>' LIMIT 5;` | derived data lives apart from raw, with lineage | processed storage |
| 8-9 | `GET /analytics/anomalies?severity=critical` in Swagger; then `docker compose --profile tools run --rm tools python -m scripts.score_anomalies data/large_truth.json` | every injected incident was found; 100% recall | the analytics are correct |
| 9-10 | `python scripts/demo.py failure` | `docker kill` at 40%; the loss; redelivery to survivors; job completes; **checksums identical to the no-failure run** | controlled failure and recovery |
| 10-11 | open `benchmarks/results.md` | the 10k / 100k / 1M table; 1 vs 4 workers; flat query latency | data-intensive requirement |
| 11-12 | `GET /api/v1/metrics` as admin; open `benchmarks/locust_high.html` | M1-M4 live; the 500-user report and where it bends | metrics and load testing |

The worker kill at minute 9 is the only point where a failure is induced in front of the evaluator. The broker waits
45 s before redelivering, so keep talking (explain `acks_late`, pure map tasks and idempotent writes while it runs).

Optional extras if there is time: `python scripts/demo.py broker` (Redis down: job `503`, ingestion still `201`) and
`python scripts/demo.py db --down-seconds 15`.

## Anticipated questions

| Question | Answer |
|---|---|
| Why not Spark or Hadoop? | At 1M rows the distribution overhead exceeds the work; a Celery chord gives the same map/shuffle/reduce model with real process parallelism. |
| Why SQL and not NoSQL? | The dominant operation is aggregation over time windows, which relational engines are built for. Redis is used where key-value is stronger: shuffle and cache. |
| How is this not CRUD? | A CRUD app never returns `202`. The write, processing and read paths are separate tiers that scale and fail independently. |
| What if two jobs cover the same window? | Result writes are `ON CONFLICT DO UPDATE` keyed on the grain, so the operation is idempotent (there is a test for it). |
| How do you know the anomaly detection is correct? | Anomalies are injected at known timestamps with a ground-truth file; detection is *scored* against it. |
| Why isn't 4 workers 4× faster? | Amdahl: the reduce stage is serial and the chord barrier waits for the slowest partition. The reduce persist was cut from 14 s to 7 s by switching to COPY + one upsert; it is still the tail. |
| What is your bottleneck? | See `benchmarks/loadtest_results.md` and the 500/1000-user reports for the measured answer. |
| How would you scale to 100M events? | Partition by week, move shuffle to object storage, shard the reduce by department, switch to Spark, add read replicas. |
| Where would this break in production? | Single region; no dead-letter queue for permanently failing partitions; the shuffle is memory-bound in Redis. |
| What happens if the database is down? | Map tasks retry with backoff (1, 2, 4, 8, 16 s); a long outage ends the job `partial` with `error_detail`; the API says `503`; nothing is silently wrong. |

## Live-modification readiness

Likely asks: a new filter on an endpoint, a fourth aggregate in the reducer, a new RBAC role. See "Extending it" in
[ARCHITECTURE.md](ARCHITECTURE.md). Adding an aggregate touches exactly three places: the map emit, the reduce fold, and
the response schema. Practise it once before the demonstration.
