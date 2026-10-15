"""Twelve-factor configuration: every setting comes from the environment and is
read exactly once into a Settings object at startup."""
from functools import lru_cache
from urllib.parse import urlparse, urlunparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Logical Redis databases. One Redis instance, four separate keyspaces.
REDIS_DB_BROKER = 0   # Celery queue
REDIS_DB_CACHE = 1    # analytics cache, metrics, rate limits, refresh tokens
REDIS_DB_BACKEND = 2  # Celery results (chord bookkeeping)
REDIS_DB_STATE = 3    # shuffle store and per-job progress

DEFAULT_JWT_SECRET = "dev-only-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    environment: str = "dev"
    service_name: str = "api"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg2://hospital:hospital@localhost:5432/hospital"
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_timeout: int = 10
    # Threads available to sync endpoints per API process (anyio default is 40). Must exceed the
    # DB pool so queued requests wait on the pool, not on a thread.
    threadpool_size: int = 64
    api_workers: int = 2

    redis_url: str = "redis://localhost:6379"
    # Shifts all four logical databases (tests use 10 so they never touch a running stack).
    redis_db_offset: int = 0

    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    bcrypt_rounds: int = 12

    cors_origins: str = "http://localhost:3000,http://localhost:8000"
    api_key_cache_seconds: int = 30
    default_rate_limit_per_min: int = 6000
    rate_limit_enabled: bool = True
    cache_ttl_seconds: int = 60

    # Processing tier
    celery_concurrency: int = 2
    # How long a dead worker's unacknowledged tasks stay invisible before the
    # broker hands them to a surviving worker. Celery's default is one hour,
    # which would make the failure demo look like a hang.
    celery_visibility_timeout: int = 45
    # Demo aid only (0 in normal operation): hold each map task for this long so a worker killed
    # mid-job is guaranteed to be holding tasks. Real map tasks take ~5 ms, so without it a kill
    # often lands between tasks and there is nothing to recover.
    map_task_sleep_ms: int = 0
    map_max_retries: int = 5
    reduce_max_retries: int = 5
    reduce_barrier_max_wait_seconds: int = 300
    shuffle_ttl_seconds: int = 6 * 3600
    max_job_span_days: int = 1500

    # Object storage (S3-compatible; MinIO locally)
    s3_endpoint_url: str | None = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "hospital-events"
    s3_region: str = "us-east-1"

    # Dev bootstrap (scripts.bootstrap). Ignored unless seed_users is true.
    seed_users: bool = False
    seed_admin_email: str = "admin@hospital.local"
    seed_admin_password: str = ""
    seed_analyst_email: str = "analyst@hospital.local"
    seed_analyst_password: str = ""
    seed_viewer_email: str = "viewer@hospital.local"
    seed_viewer_password: str = ""
    seed_ingest_api_key: str = ""

    @field_validator("database_url")
    @classmethod
    def _normalise_database_url(cls, v: str) -> str:
        """Managed Postgres providers hand out postgres:// or postgresql:// URLs; SQLAlchemy 2
        rejects the first and would default the driver on the second. Pin the driver we ship."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg2://" + v[len(prefix):]
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_dev(self) -> bool:
        return self.environment.lower() in {"dev", "development", "test"}

    def redis_url_for(self, db: int) -> str:
        """Same Redis, different logical database. Works for redis:// and rediss://."""
        parsed = urlparse(self.redis_url)
        return urlunparse(parsed._replace(path=f"/{db + self.redis_db_offset}"))

    @property
    def broker_url(self) -> str:
        return self.redis_url_for(REDIS_DB_BROKER)

    @property
    def backend_url(self) -> str:
        return self.redis_url_for(REDIS_DB_BACKEND)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.is_dev and s.jwt_secret == DEFAULT_JWT_SECRET:
        raise RuntimeError("JWT_SECRET must be set outside the dev environment")
    return s


settings = get_settings()
