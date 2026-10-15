"""Operational metrics (admin only).

M1 job processing time   M2 ingestion throughput   M3 API latency p50/p95/p99   M4 cache hit ratio
"""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.db import get_db
from app.core.deps import Principal, require_role
from app.core.security import Role

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("", summary="Operational metrics: latencies, throughput, cache hit ratio (admin)")
def get_metrics(_: Principal = Depends(require_role(Role.ADMIN)), db: Session = Depends(get_db)):
    by_type = db.execute(
        text(
            """
            SELECT job_type,
                   count(*)                                   AS jobs,
                   round(avg(duration_seconds), 3)::float8    AS avg_seconds,
                   min(duration_seconds)::float8              AS min_seconds,
                   max(duration_seconds)::float8              AS max_seconds,
                   round(sum(events_processed) / NULLIF(sum(duration_seconds), 0))::float8 AS events_per_second
            FROM analytics.analysis_jobs
            WHERE status = 'completed'
            GROUP BY job_type ORDER BY job_type
            """
        )
    ).mappings().all()
    recent = db.execute(
        text(
            """
            SELECT job_id::text AS job_id, job_type, status, partitions_total, events_processed,
                   duration_seconds::float8 AS duration_seconds,
                   round(events_processed / NULLIF(duration_seconds, 0))::float8 AS events_per_second
            FROM analytics.analysis_jobs
            WHERE finished_at IS NOT NULL
            ORDER BY finished_at DESC LIMIT 10
            """
        )
    ).mappings().all()
    batches = db.execute(
        text(
            """
            SELECT batch_id::text AS batch_id, row_count,
                   round(EXTRACT(EPOCH FROM (finished_at - started_at))::numeric, 2)::float8 AS seconds,
                   round(row_count / NULLIF(EXTRACT(EPOCH FROM (finished_at - started_at)), 0))::float8 AS rows_per_second
            FROM raw.ingest_batches
            WHERE status = 'completed'
            ORDER BY finished_at DESC LIMIT 10
            """
        )
    ).mappings().all()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "M1_job_processing_time": {"by_job_type": [dict(r) for r in by_type], "recent_jobs": [dict(r) for r in recent]},
        "M2_ingestion_throughput": {"single_events": metrics.ingest_rate(), "bulk_batches": [dict(r) for r in batches]},
        "M3_api_latency_ms": metrics.latency_summary(),
        "M4_cache": metrics.cache_summary(),
        "responses_by_status": metrics.status_counts(),
    }
