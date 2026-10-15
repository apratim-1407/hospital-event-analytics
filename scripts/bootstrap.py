"""Idempotent first-run setup, run after migrations (docker compose `migrate` service).

* creates the object-storage bucket
* when SEED_USERS=true (dev): creates admin / analyst / viewer accounts and the
  ingest API key supplied in SEED_INGEST_API_KEY

Production: leave SEED_USERS unset, create the first admin with `scripts.create_admin`
and mint ingest keys through POST /api/v1/admin/api-keys.
"""
import structlog
from sqlalchemy import select

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.logging import configure_logging
from app.core.security import hash_api_key, hash_password
from app.models.auth import ApiKey, User
from app.services import storage

configure_logging()
log = structlog.get_logger("bootstrap")


def ensure_user(db, email: str, password: str, role: str) -> None:
    if not email or not password:
        return
    if db.execute(select(User).where(User.email == email.lower())).scalar_one_or_none():
        return
    db.add(User(email=email.lower(), password_hash=hash_password(password), role=role))
    db.commit()
    log.info("bootstrap.user_created", role=role)


def ensure_api_key(db, plaintext: str, name: str) -> None:
    if not plaintext:
        return
    digest = hash_api_key(plaintext)
    if db.execute(select(ApiKey).where(ApiKey.key_hash == digest)).scalar_one_or_none():
        return
    db.add(ApiKey(name=name, key_prefix=plaintext[:8], key_hash=digest, scope="ingest"))
    db.commit()
    log.info("bootstrap.api_key_created", name=name)


def main() -> None:
    try:
        storage.ensure_bucket()
        log.info("bootstrap.bucket_ready", bucket=settings.s3_bucket)
    except Exception as exc:  # object storage is optional for everything except bulk load
        log.warning("bootstrap.bucket_unavailable", error=type(exc).__name__)

    if not settings.seed_users:
        log.info("bootstrap.seed_skipped")
        return
    if not settings.is_dev:
        raise SystemExit("SEED_USERS is for development only; refusing to seed in this environment")
    with SessionLocal() as db:
        ensure_user(db, settings.seed_admin_email, settings.seed_admin_password, "admin")
        ensure_user(db, settings.seed_analyst_email, settings.seed_analyst_password, "analyst")
        ensure_user(db, settings.seed_viewer_email, settings.seed_viewer_password, "viewer")
        ensure_api_key(db, settings.seed_ingest_api_key, "seeded-dev-ingest")


if __name__ == "__main__":
    main()
