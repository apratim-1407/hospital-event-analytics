# Benchmark results

Recorded 2026-10-14T19:55:10+00:00 on Windows-11-10.0.22631-SP0 (16 CPUs visible to Docker). Each worker container runs 2 processes.

## Processing time (M1), full analysis job

| Dataset | Events | Partitions | 1 worker (s) | 2 workers (s) | 4 workers (s) | Speed-up (1->4) | Throughput @4w (events/s) |
|---|---|---|---|---|---|---|---|
| small | 10,000 | 248 | 1.2 | 0.9 | 0.7 | 1.64x | 13,808 |
| medium | 100,000 | 1,448 | 7.1 | 4.9 | 3.7 | 1.90x | 26,829 |
| large | 1,000,000 | 8,768 | 43.4 | 28.6 | 23.7 | 1.83x | 42,230 |

## Query latency vs raw data volume (GET /analytics/admissions, 7 days x 8 depts)

| Dataset | Raw events | p50 (ms) | p95 (ms) |
|---|---|---|---|
| small | 10,000 | 3.98 | 25.84 |
| medium | 100,000 | 4.69 | 25.78 |
| large | 1,000,000 | 4.83 | 25.26 |

Latency is client-side over HTTP on localhost with the cache bypassed. It reads pre-aggregated tables, so it depends on the requested range, not on raw volume.
