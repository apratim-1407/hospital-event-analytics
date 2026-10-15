"""Test harness.

Unit tests (anomaly, partition, map/reduce logic, generator) need nothing external.
Integration tests (`db` marker fixtures) use a throwaway Postgres database
(`hospital_test`, rebuilt from the Alembic migrations each session) and Redis logical
databases 10-13, so a running dev stack is never touched.
"""
import os

# Must be set before anything imports app.core.config.
os.environ["ENVIRONMENT"] = "test"
os.environ["BCRYPT_ROUNDS"] = "4"
os.environ["REDIS_DB_OFFSET"] = "10"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["API_KEY_CACHE_SECONDS"] = "0"
os.environ["LOG_LEVEL"] = "WARNING"
_base_url = os.environ.get("DATABASE_URL", "postgresql+psycopg2://hospital:hospital@postgres:5432/hospital")
TEST_DB = "hospital_test"
os.environ["DATABASE_URL"] = _base_url.rsplit("/", 1)[0] + "/" + TEST_DB

import psycopg2  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402


def _admin_dsn() -> dict:
    u = make_url(_base_url)
    return dict(host=u.host, port=u.port or 5432, user=u.username, password=u.password, dbname="postgres")


@pytest.fixture(scope="session")
def database():
    """Fresh schema from the real migrations."""
    conn = psycopg2.connect(**_admin_dsn())
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    cur.execute(f"CREATE DATABASE {TEST_DB}")
    conn.close()

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    yield TEST_DB


@pytest.fixture(scope="session")
def redis_clean(database):
    from app.core.redis import get_redis

    for db in range(4):
        get_redis(db).flushdb()
    yield


@pytest.fixture(scope="session")
def app(database, redis_clean):
    from app.main import app as fastapi_app

    return fastapi_app


@pytest.fixture(scope="session")
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def seeded(database):
    """Three users (one per role) and an ingest API key."""
    from app.core.db import SessionLocal
    from app.core.security import generate_api_key, hash_api_key, hash_password
    from app.models.auth import ApiKey, User

    creds = {}
    with SessionLocal() as db:
        for role in ("viewer", "analyst", "admin"):
            email, pw = f"{role}@test.local", f"{role.capitalize()}#Test12345"
            db.add(User(email=email, password_hash=hash_password(pw), role=role))
            creds[role] = (email, pw)
        key = generate_api_key()
        db.add(ApiKey(name="test", key_prefix=key[:8], key_hash=hash_api_key(key), rate_limit_per_min=100000))
        db.commit()
    creds["api_key"] = key
    return creds


@pytest.fixture(scope="session")
def tokens(client, seeded):
    out = {}
    for role in ("viewer", "analyst", "admin"):
        email, pw = seeded[role]
        r = client.post("/api/v1/auth/login", json={"email": email, "password": pw})
        assert r.status_code == 200, r.text
        out[role] = r.json()
    return out


@pytest.fixture
def auth(tokens):
    def _h(role: str) -> dict:
        return {"Authorization": f"Bearer {tokens[role]['access_token']}"}

    return _h


@pytest.fixture
def key_header(seeded):
    return {"X-API-Key": seeded["api_key"]}
