"""60-second Redis cache for analytics reads.

The cache key embeds a version counter that the reducer bumps when a job finishes,
so new results are visible immediately instead of waiting out the TTL. All access
is fail-open: if Redis is down, reads fall through to Postgres.
"""
import hashlib
import json

from app.core import metrics
from app.core.config import settings
from app.core.redis import guarded

VERSION_KEY = "ac:version"


def _version() -> str:
    return guarded(lambda r: r.get(VERSION_KEY), default="0") or "0"


def bump_analytics_version() -> None:
    guarded(lambda r: r.incr(VERSION_KEY))


def make_key(endpoint: str, params: dict) -> str:
    blob = json.dumps(params, sort_keys=True, default=str)
    digest = hashlib.sha1(blob.encode()).hexdigest()
    return f"ac:{_version()}:{endpoint}:{digest}"


def get_cached(key: str) -> str | None:
    """The cached response body, already serialised JSON."""
    raw = guarded(lambda r: r.get(key), default=None)
    metrics.record_cache(hit=raw is not None)
    return raw


def set_cached(key: str, body: str) -> None:
    guarded(lambda r: r.set(key, body, ex=settings.cache_ttl_seconds))
