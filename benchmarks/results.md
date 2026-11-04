# Benchmark results

Recorded 2026-11-04T08:24:58+00:00 on Windows-11-10.0.22631-SP0 (16 CPUs visible to Docker). Each worker container runs 2 processes.

## Processing time (M1), full analysis job

| Dataset | Events | Partitions | 1 worker (s) | 2 workers (s) | 4 workers (s) | Speed-up (1->4) | Throughput @4w (events/s) |
|---|---|---|---|---|---|---|---|
| small | 10,000 | 248 | 1.4 | 1.0 | 0.8 | 1.68x | 11,820 |
| medium | 100,000 | 1,448 | 8.8 | 5.8 | 4.3 | 2.02x | 23,063 |
| large | 1,000,000 | 8,768 | 44.9 | 30.4 | 26.6 | 1.69x | 37,632 |

## Query latency vs raw data volume (GET /analytics/admissions, 7 days x 8 depts)

| Dataset | Raw events | p50 (ms) | p95 (ms) |
|---|---|---|---|
| small | 10,000 | 8.13 | 29.77 |
| medium | 100,000 | 9.15 | 31.52 |
| large | 1,000,000 | 4.19 | 23.8 |

Latency is client-side over HTTP on localhost with the cache bypassed. It reads pre-aggregated tables, so it depends on the requested range, not on raw volume.
