"""Redis access.

Two usage styles:

* `get_redis(db)` for state the system cannot work without (shuffle store, job
  progress, refresh tokens). Errors propagate so callers can retry.
* `guarded(op, default)` for state that merely improves the system (cache,
  metrics, rate limits). It fails open: if Redis is unreachable the operation is
  skipped and a short circuit-breaker stops every request paying the connect
  timeout. This is what keeps ingestion alive while the broker is down.
"""
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import redis
from redis.exceptions import RedisError

from app.core.config import (
    REDIS_DB_CACHE,
    REDIS_DB_STATE,
    settings,
)

T = TypeVar("T")

_clients: dict[int, redis.Redis] = {}
_lock = threading.Lock()
_down_until = 0.0
BREAKER_SECONDS = 3.0


def get_redis(db: int = REDIS_DB_CACHE) -> redis.Redis:
    client = _clients.get(db)
    if client is None:
        with _lock:
            client = _clients.get(db)
            if client is None:
                client = redis.Redis.from_url(
                    settings.redis_url_for(db),
                    decode_responses=True,
                    socket_connect_timeout=1.0,
                    socket_timeout=3.0,
                    max_connections=100,
                )
                _clients[db] = client
    return client


def get_state_redis() -> redis.Redis:
    return get_redis(REDIS_DB_STATE)


def redis_available() -> bool:
    return time.monotonic() >= _down_until


def guarded(op: Callable[[redis.Redis], T], default: T | None = None, db: int = REDIS_DB_CACHE) -> T | None:
    """Run `op(client)`; swallow Redis failures and trip the breaker."""
    global _down_until
    if time.monotonic() < _down_until:
        return default
    try:
        return op(get_redis(db))
    except (RedisError, OSError):
        _down_until = time.monotonic() + BREAKER_SECONDS
        return default


def ping() -> bool:
    try:
        return bool(get_redis(REDIS_DB_CACHE).ping())
    except (RedisError, OSError):
        return False
