"""Authentication and authorization as FastAPI dependencies.

RBAC is enforced here rather than with an `if` inside each handler, so a new
endpoint cannot forget it:

    @router.post("/jobs", dependencies=[Depends(require_role(Role.ANALYST))])
"""
import time
from dataclasses import dataclass

import structlog
from fastapi import Depends, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.errors import APIError
from app.core.redis import guarded
from app.core.security import Role, decode_token, hash_api_key
from app.models.auth import ApiKey

log = structlog.get_logger("auth")
_bearer = HTTPBearer(auto_error=False, description="Access token from POST /api/v1/auth/login")


@dataclass(frozen=True)
class Principal:
    user_id: int
    role: Role


@dataclass(frozen=True)
class ApiKeyPrincipal:
    key_id: int
    name: str
    scope: str
    rate_limit_per_min: int


def _remember_principal(request: Request, label: str) -> None:
    """Expose who made the call to the access-log middleware (contextvars set in a
    threadpool dependency do not flow back out, scope state does) and to later logs."""
    request.scope.setdefault("state", {})["principal"] = label
    structlog.contextvars.bind_contextvars(principal=label)


async def get_current_user(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    # Pure CPU (a JWT signature check): async so it costs no thread-pool hop.
    if creds is None:
        raise APIError(401, "unauthenticated", "Missing bearer token", {"WWW-Authenticate": "Bearer"})
    payload = decode_token(creds.credentials, "access")
    try:
        principal = Principal(user_id=int(payload["sub"]), role=Role(payload["role"]))
    except (KeyError, ValueError):
        raise APIError(401, "invalid_token", "Invalid token claims", {"WWW-Authenticate": "Bearer"})
    _remember_principal(request, f"user:{principal.user_id}:{principal.role.value}")
    return principal


def require_role(minimum: Role):
    """Dependency factory: viewer < analyst < admin."""

    async def dependency(principal: Principal = Depends(get_current_user)) -> Principal:
        if principal.role.rank < minimum.rank:
            log.warning(
                "auth.role_denied",
                user_id=principal.user_id,
                role=principal.role.value,
                required=minimum.value,
            )
            raise APIError(
                403, "forbidden", f"This action requires the '{minimum.value}' role or higher"
            )
        return principal

    return dependency


# ---------------------------------------------------------------- API keys
# Valid keys are cached in-process for a short time so ingestion does not pay a
# database round trip per event. Revocation therefore takes up to
# api_key_cache_seconds to reach every replica.
_key_cache: dict[str, tuple[float, ApiKeyPrincipal]] = {}


def clear_api_key_cache() -> None:
    _key_cache.clear()


_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end
tokens = math.min(capacity, tokens + (now - ts) * rate)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 120)
return {allowed, math.floor(tokens)}
"""


def _check_rate_limit(principal: ApiKeyPrincipal) -> None:
    """Redis token bucket per API key. Fails open if Redis is unavailable."""
    if not settings.rate_limit_enabled:
        return
    capacity = principal.rate_limit_per_min
    rate = capacity / 60.0

    def op(r):
        return r.eval(_BUCKET_LUA, 1, f"rl:{principal.key_id}", capacity, rate, time.time())

    result = guarded(op, default=None)
    if result is not None and int(result[0]) == 0:
        log.warning("ingest.rate_limited", key_id=principal.key_id, key_name=principal.name)
        raise APIError(429, "rate_limited", "Rate limit exceeded for this API key", {"Retry-After": "1"})


def _lookup_api_key(digest: str) -> ApiKeyPrincipal | None:
    with SessionLocal() as db:
        row = db.execute(select(ApiKey).where(ApiKey.key_hash == digest, ApiKey.is_active.is_(True))).scalar_one_or_none()
        if row is None:
            return None
        principal = ApiKeyPrincipal(row.key_id, row.name, row.scope, row.rate_limit_per_min)
        db.execute(update(ApiKey).where(ApiKey.key_id == row.key_id).values(last_used_at=_utcnow()))
        db.commit()
        return principal


async def get_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> ApiKeyPrincipal:
    if not x_api_key:
        raise APIError(401, "unauthenticated", "Missing X-API-Key header")
    digest = hash_api_key(x_api_key)
    now = time.monotonic()
    cached = _key_cache.get(digest)
    if cached and cached[0] > now:
        principal = cached[1]
    else:
        principal = await run_in_threadpool(_lookup_api_key, digest)  # DB round trip: only on a cache miss
        if principal is None:
            _key_cache.pop(digest, None)
            log.warning("auth.api_key_rejected")
            raise APIError(401, "invalid_api_key", "Invalid or revoked API key")
        _key_cache[digest] = (now + settings.api_key_cache_seconds, principal)
    _remember_principal(request, f"apikey:{principal.key_id}")
    if settings.rate_limit_enabled:
        await run_in_threadpool(_check_rate_limit, principal)  # Redis Lua: blocking I/O stays off the event loop
    return principal


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)
