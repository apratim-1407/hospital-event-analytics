from fastapi import APIRouter, Depends
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import structlog

from app.core.db import get_db
from app.core.errors import APIError
from app.core.security import (
    hash_password,
    issue_token_pair,
    rotate_refresh_token,
    verify_password,
)
from app.models.auth import User
from app.schemas.auth import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse, UserOut
from app.schemas.common import ErrorEnvelope

log = structlog.get_logger("auth")
router = APIRouter(prefix="/auth", tags=["auth"])

# Verified against when the account does not exist, so response time does not reveal
# which emails are registered.
_DUMMY_HASH = hash_password("not-a-real-password")


def _token_response(user_id: int, role: str) -> TokenResponse:
    try:
        pair = issue_token_pair(user_id, role)
    except (RedisError, OSError):
        raise APIError(503, "service_unavailable", "Session store is unavailable; please retry shortly")
    return TokenResponse(access_token=pair.access_token, refresh_token=pair.refresh_token, expires_in=pair.expires_in)


@router.post(
    "/register",
    response_model=UserOut,
    status_code=201,
    summary="Create an account (viewer role; an admin grants more)",
    responses={409: {"model": ErrorEnvelope}},
)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    user = User(email=body.email, password_hash=hash_password(body.password), role="viewer")
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise APIError(409, "conflict", "An account with this email already exists")
    log.info("auth.registered", user_id=user.user_id, role=user.role)
    return UserOut(user_id=user.user_id, email=user.email, role=user.role, is_active=user.is_active, created_at=user.created_at)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange credentials for an access + refresh token pair",
    responses={401: {"model": ErrorEnvelope}},
)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    row = db.execute(select(User).where(User.email == body.email)).scalar_one_or_none()
    found = (row.user_id, row.password_hash, row.role, row.is_active) if row else None
    # Return the pooled connection BEFORE the ~250 ms bcrypt: hashing is CPU work and must not
    # pin a scarce connection (500 simultaneous logins would otherwise exhaust the pool).
    db.close()
    ok = verify_password(body.password, found[1] if found else _DUMMY_HASH)
    if not found or not ok or not found[3]:
        # Same message for every failure; the reason goes to the log, never the client.
        reason = "unknown_user" if not found else ("inactive" if not found[3] else "bad_password")
        log.warning("auth.login_failed", reason=reason)
        raise APIError(401, "invalid_credentials", "Invalid email or password")
    log.info("auth.login_succeeded", user_id=found[0], role=found[2])
    return _token_response(found[0], found[2])


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate a refresh token (reuse of a spent token revokes the whole session)",
    responses={401: {"model": ErrorEnvelope}},
)
def refresh(body: RefreshRequest, db: Session = Depends(get_db)):
    def lookup(user_id: int):
        u = db.get(User, user_id)
        return (u.role, u.is_active) if u else None

    try:
        pair = rotate_refresh_token(body.refresh_token, lookup)
    except (RedisError, OSError):
        raise APIError(503, "service_unavailable", "Session store is unavailable; please retry shortly")
    log.info("auth.token_refreshed")
    return TokenResponse(access_token=pair.access_token, refresh_token=pair.refresh_token, expires_in=pair.expires_in)
