"""Stage 3: shuffle.

Map output is written to Redis under  job:{job_id}:key:{date}:{department}  so
grouping by reducer key happens implicitly through the key name. Map tasks
pre-aggregate inside their own partition before emitting (a "combiner"), so the
shuffle holds a few thousand small keys rather than millions of rows.

Writes are idempotent: re-running a partition overwrites the same key and adds
the same member to the done-set, so redelivered tasks cannot double-count.
"""
import json

from app.core.config import settings
from app.core.redis import get_state_redis

READ_CHUNK = 500


def _key(job_id: str, partition_key: str) -> str:
    return f"job:{job_id}:key:{partition_key}"


def write_accumulator(job_id: str, partition_key: str, acc: dict) -> None:
    r = get_state_redis()
    pipe = r.pipeline()
    pipe.set(_key(job_id, partition_key), json.dumps(acc, separators=(",", ":")), ex=settings.shuffle_ttl_seconds)
    pipe.sadd(f"job:{job_id}:done", partition_key)
    pipe.expire(f"job:{job_id}:done", settings.shuffle_ttl_seconds)
    pipe.execute()


def mark_failed(job_id: str, partition_key: str, error: str) -> None:
    r = get_state_redis()
    pipe = r.pipeline()
    pipe.hset(f"job:{job_id}:failed", partition_key, error[:300])
    pipe.expire(f"job:{job_id}:failed", settings.shuffle_ttl_seconds)
    pipe.execute()


def done_count(job_id: str) -> int:
    return int(get_state_redis().scard(f"job:{job_id}:done"))


def done_keys(job_id: str) -> set[str]:
    return set(get_state_redis().smembers(f"job:{job_id}:done"))


def failed_partitions(job_id: str) -> dict[str, str]:
    return get_state_redis().hgetall(f"job:{job_id}:failed")


def read_accumulators(job_id: str, partition_keys: list[str]) -> dict[str, dict | None]:
    """Fetch accumulators; a missing key comes back as None."""
    r = get_state_redis()
    out: dict[str, dict | None] = {}
    for i in range(0, len(partition_keys), READ_CHUNK):
        chunk = partition_keys[i : i + READ_CHUNK]
        values = r.mget([_key(job_id, k) for k in chunk])
        for k, v in zip(chunk, values):
            out[k] = json.loads(v) if v is not None else None
    return out


def is_cancelled(job_id: str) -> bool:
    return bool(get_state_redis().exists(f"job:{job_id}:cancelled"))


def set_cancelled(job_id: str) -> None:
    get_state_redis().set(f"job:{job_id}:cancelled", "1", ex=settings.shuffle_ttl_seconds)


def set_failure_note(job_id: str, note: dict) -> None:
    """Last-resort record when the database itself is unreachable."""
    get_state_redis().set(f"job:{job_id}:failure", json.dumps(note), ex=7 * 86400)


def get_failure_note(job_id: str) -> dict | None:
    raw = get_state_redis().get(f"job:{job_id}:failure")
    return json.loads(raw) if raw else None
