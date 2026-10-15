from collections.abc import AsyncIterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# The engine is lazy: nothing connects until first use, so forking Celery
# workers after import is safe. Sessions are always in UTC. A short connect timeout makes a
# dead database fail fast (and be retried by the caller) instead of hanging a worker.
engine = create_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_pre_ping=True,
    connect_args={
        "options": "-c timezone=UTC",
        "connect_timeout": 3,
        "keepalives": 1,
        "keepalives_idle": 5,
        "keepalives_interval": 2,
        "keepalives_count": 3,
    },
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncIterator[Session]:
    """FastAPI dependency: one session per request.

    Deliberately an *async* generator. A sync generator dependency runs its setup and teardown
    in the same 40-thread pool as the endpoints; under load every thread ends up parked waiting
    for a pooled connection while the teardown that would release one waits for a free thread,
    and the whole service stalls until the pool timeout. A Session opens no connection until its
    first query (which happens inside the endpoint's own thread), and close() just returns the
    connection, so neither step needs a thread."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
