"""Request edge: correlation ID, structured access log, latency metric.

Pure ASGI (not BaseHTTPMiddleware) so the per-request overhead stays small
under load testing, and so contextvars bound here are visible to handlers.
"""
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import metrics

log = structlog.get_logger("http")

CORRELATION_HEADER = "x-correlation-id"
QUIET_PATHS = {"/health"}


def _valid_uuid(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"] if k == CORRELATION_HEADER.encode()}
        correlation_id = _valid_uuid(raw.get(CORRELATION_HEADER)) or str(uuid.uuid4())
        scope.setdefault("state", {})["correlation_id"] = correlation_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                MutableHeaders(scope=message)[CORRELATION_HEADER] = correlation_id
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            endpoint = f"{scope['method']} {template}"
            status = status_holder["status"]
            fields = {
                "method": scope["method"],
                "path": scope["path"],
                "status": status,
                "duration_ms": round(duration_ms, 2),
                "principal": (scope.get("state") or {}).get("principal"),
            }
            if scope["path"] in QUIET_PATHS or scope["path"].startswith("/app/"):
                log.debug("http.request", **fields)
            else:
                level = "warning" if status >= 400 else "info"
                getattr(log, level)("http.request", **fields)
                if template != "unmatched":
                    metrics.record_request(endpoint, status, duration_ms)
