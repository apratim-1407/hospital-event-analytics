"""Credentials.

Humans: bcrypt-hashed passwords, short-lived HS256 access tokens, and refresh
tokens that rotate on use. A refresh token that is presented twice means it was
copied, so the whole token family is revoked.

Machines: random API keys, shown once at creation and stored only as a SHA-256
hash. A key is independently revocable and carries no user identity, which is
the correct shape for a hospital system streaming events with no interactive
login.
"""
import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

import bcrypt
import jwt

from app.core.config import settings
from app.core.errors import APIError
from app.core.redis import get_redis

MAX_PASSWORD_BYTES = 72  # bcrypt's hard input limit
API_KEY_PREFIX = "hea_"


class Role(str, Enum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return {"viewer": 1, "analyst": 2, "admin": 3}[self.value]


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# ------------------------------------------------------------------- tokens
@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(user_id: int, role: str) -> str:
    now = _now()
    payload = {
        "sub": str(user_id),
        "role": role,
        "typ": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _create_refresh_token(user_id: int, role: str, family: str) -> tuple[str, int]:
    now = _now()
    ttl = timedelta(days=settings.refresh_token_days)
    payload = {
        "sub": str(user_id),
        "role": role,
        "typ": "refresh",
        "fam": family,
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, int(ttl.total_seconds())


def decode_token(token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError:
        raise APIError(401, "token_expired", "Token has expired", {"WWW-Authenticate": "Bearer"})
    except jwt.PyJWTError:
        raise APIError(401, "invalid_token", "Invalid token", {"WWW-Authenticate": "Bearer"})
    if payload.get("typ") != expected_type:
        raise APIError(401, "invalid_token", f"Expected a {expected_type} token", {"WWW-Authenticate": "Bearer"})
    return payload


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def issue_token_pair(user_id: int, role: str, family: str | None = None) -> TokenPair:
    """Mint an access + refresh pair. Refresh tokens are stored hashed in Redis."""
    family = family or uuid.uuid4().hex
    refresh, ttl = _create_refresh_token(user_id, role, family)
    r = get_redis()
    pipe = r.pipeline()
    pipe.set(f"rt:{_sha256(refresh)}", json.dumps({"user_id": user_id, "family": family}), ex=ttl)
    pipe.sadd(f"rtfam:{family}", _sha256(refresh))
    pipe.expire(f"rtfam:{family}", ttl)
    pipe.execute()
    return TokenPair(
        access_token=create_access_token(user_id, role),
        refresh_token=refresh,
        expires_in=settings.access_token_minutes * 60,
    )


def rotate_refresh_token(refresh_token: str, current_role_lookup) -> TokenPair:
    """Exchange a refresh token for a new pair. Reuse of a spent token revokes the family.

    `current_role_lookup(user_id) -> (role, is_active) | None` re-reads the user so a role
    change or deactivation takes effect at the next refresh.
    """
    payload = decode_token(refresh_token, "refresh")
    family = payload["fam"]
    r = get_redis()
    digest = _sha256(refresh_token)

    if r.exists(f"rtrev:{family}"):
        raise APIError(401, "token_revoked", "Refresh token family has been revoked")

    # SET NX is the atomic "spend" operation: the second presenter loses.
    spent = r.set(f"rtused:{digest}", "1", nx=True, ex=settings.refresh_token_days * 86400)
    if not spent:
        revoke_family(family)
        raise APIError(401, "token_reuse_detected", "Refresh token reuse detected; session revoked")
    if not r.exists(f"rt:{digest}"):
        raise APIError(401, "invalid_token", "Refresh token is unknown or expired")

    found = current_role_lookup(int(payload["sub"]))
    if found is None or not found[1]:
        revoke_family(family)
        raise APIError(401, "invalid_token", "Account is unavailable")
    return issue_token_pair(int(payload["sub"]), found[0], family=family)


def revoke_family(family: str) -> None:
    r = get_redis()
    members = r.smembers(f"rtfam:{family}")
    pipe = r.pipeline()
    for digest in members:
        pipe.delete(f"rt:{digest}")
    pipe.set(f"rtrev:{family}", "1", ex=settings.refresh_token_days * 86400)
    pipe.delete(f"rtfam:{family}")
    pipe.execute()


# ----------------------------------------------------------------- API keys
def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return _sha256(key)
