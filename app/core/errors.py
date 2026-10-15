"""One error envelope for every failure:
{ "error": { "code", "message", "correlation_id" } }
so any failed request can be found in the logs immediately."""
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger("errors")

_CODE_BY_STATUS = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "unprocessable",
    429: "rate_limited",
    503: "service_unavailable",
}


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None, details=None):
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers
        self.details = details


def _cid(request: Request) -> str | None:
    state = request.scope.get("state") or {}
    return state.get("correlation_id")


def _envelope(request: Request, status: int, code: str, message: str, headers=None, details=None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message, "correlation_id": _cid(request)}}
    if details is not None:
        body["error"]["details"] = details
    resp = JSONResponse(status_code=status, content=body, headers=headers)
    return resp


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError):
        return _envelope(request, exc.status, exc.code, exc.message, exc.headers, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = _CODE_BY_STATUS.get(exc.status_code, "error")
        return _envelope(request, exc.status_code, code, str(exc.detail), getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
            for e in exc.errors()
        ]
        return _envelope(request, 400, "validation_error", "Request validation failed", details=details)

    @app.exception_handler(OperationalError)
    async def _database_down(request: Request, exc: OperationalError):
        log.error("database.unavailable", path=request.url.path, error=type(exc.orig).__name__ if exc.orig else "OperationalError")
        return _envelope(
            request, 503, "service_unavailable", "The database is temporarily unavailable; please retry shortly",
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(PoolTimeoutError)
    async def _pool_exhausted(request: Request, exc: PoolTimeoutError):
        # Every pooled connection was busy for the whole wait: shed load honestly.
        log.warning("database.pool_exhausted", path=request.url.path)
        return _envelope(
            request, 503, "service_unavailable", "The service is at capacity; please retry shortly",
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.error("request.unhandled_exception", exc_info=exc, path=request.url.path)
        return _envelope(request, 500, "internal_error", "Internal server error")
