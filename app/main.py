from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.v1.router import api_router
from app.core import metrics
from app.core import redis as redis_core
from app.core.config import settings
from app.core.db import engine
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware

configure_logging()
log = structlog.get_logger("app")

DESCRIPTION = """
Cloud-deployed batch analytics over a large stream of synthetic hospital events.

* **Ingest plane** - `POST /events` and `POST /events/batch` append to an immutable raw log.
* **Processing plane** - `POST /jobs` returns **202 Accepted**; a Celery worker pool runs a
  MapReduce-style pipeline (partition by day x department, map, shuffle, reduce) and writes
  derived result tables.
* **Query plane** - `GET /analytics/*` serves pre-computed results in milliseconds.

Authenticate humans with `POST /auth/login` (Bearer JWT). Event sources authenticate with an
`X-API-Key` header.
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    import anyio.to_thread

    anyio.to_thread.current_default_thread_limiter().total_tokens = settings.threadpool_size
    metrics.start_flusher()
    log.info("api.started", environment=settings.environment, db_pool=settings.db_pool_size, threads=settings.threadpool_size)
    yield
    metrics.flush()
    log.warning("api.shutdown")
    engine.dispose()


app = FastAPI(
    title="Hospital Event Analytics",
    version="1.0.0",
    description=DESCRIPTION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Correlation-ID"],
    expose_headers=["X-Correlation-ID", "X-Cache"],
)
app.add_middleware(RequestContextMiddleware)  # outermost: wraps everything, including CORS
install_error_handlers(app)
app.include_router(api_router)

# The web UI: plain HTML/CSS/JS, no build step, served from the same origin as the API.
WEB_DIR = Path(__file__).parent / "web"
app.mount("/app", StaticFiles(directory=WEB_DIR, html=True), name="ui")


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/app/")


@app.get("/health", tags=["ops"], summary="Liveness/readiness for platform health checks")
def health():
    """200 while Postgres is reachable. A down Redis degrades analytics caching and job
    submission but not ingestion, so it is reported rather than failing the check."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    redis_ok = redis_core.ping()
    body = {"status": "ok" if db_ok else "unavailable", "database": db_ok, "redis": redis_ok}
    return JSONResponse(body, status_code=200 if db_ok else 503)
