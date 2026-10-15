"""Admin: manage users (roles) and ingest API keys."""
import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import Principal, clear_api_key_cache, require_role
from app.core.errors import APIError
from app.core.security import Role, generate_api_key, hash_api_key
from app.models.auth import ApiKey, User
from app.schemas.auth import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, UserOut, UserUpdate
from app.schemas.common import ErrorEnvelope

log = structlog.get_logger("admin")
router = APIRouter(prefix="/admin", tags=["admin"])


def _user_out(u: User) -> UserOut:
    return UserOut(user_id=u.user_id, email=u.email, role=u.role, is_active=u.is_active, created_at=u.created_at)


def _key_out(k: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        key_id=k.key_id, name=k.name, key_prefix=k.key_prefix, scope=k.scope,
        rate_limit_per_min=k.rate_limit_per_min, is_active=k.is_active, created_at=k.created_at,
        last_used_at=k.last_used_at, revoked_at=k.revoked_at,
    )


@router.get("/users", response_model=list[UserOut], summary="List users (admin)")
def list_users(_: Principal = Depends(require_role(Role.ADMIN)), db: Session = Depends(get_db)):
    return [_user_out(u) for u in db.execute(select(User).order_by(User.user_id)).scalars()]


@router.patch(
    "/users/{user_id}",
    response_model=UserOut,
    summary="Change a user's role or active flag (admin)",
    responses={404: {"model": ErrorEnvelope}},
)
def update_user(
    user_id: int,
    body: UserUpdate,
    admin: Principal = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise APIError(404, "not_found", f"User {user_id} not found")
    if user.user_id == admin.user_id and (body.is_active is False or (body.role and body.role != Role.ADMIN)):
        raise APIError(409, "conflict", "Admins cannot demote or deactivate themselves")
    if body.role is not None:
        user.role = body.role.value
    if body.is_active is not None:
        user.is_active = body.is_active
    db.commit()
    log.info("admin.user_updated", target_user_id=user_id, role=user.role, is_active=user.is_active, by=admin.user_id)
    return _user_out(user)


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=201,
    summary="Create an ingest API key; the plaintext is shown once (admin)",
)
def create_api_key(
    body: ApiKeyCreate, admin: Principal = Depends(require_role(Role.ADMIN)), db: Session = Depends(get_db)
):
    plaintext = generate_api_key()
    key = ApiKey(
        name=body.name,
        key_prefix=plaintext[:8],
        key_hash=hash_api_key(plaintext),
        rate_limit_per_min=body.rate_limit_per_min,
        created_by=admin.user_id,
    )
    db.add(key)
    db.commit()
    log.info("admin.api_key_created", key_id=key.key_id, name=key.name, by=admin.user_id)
    return ApiKeyCreated(**_key_out(key).model_dump(), api_key=plaintext)


@router.get("/api-keys", response_model=list[ApiKeyOut], summary="List API keys, hashes never shown (admin)")
def list_api_keys(_: Principal = Depends(require_role(Role.ADMIN)), db: Session = Depends(get_db)):
    return [_key_out(k) for k in db.execute(select(ApiKey).order_by(ApiKey.key_id)).scalars()]


@router.delete(
    "/api-keys/{key_id}",
    response_model=ApiKeyOut,
    summary="Revoke an API key (admin)",
    responses={404: {"model": ErrorEnvelope}},
)
def revoke_api_key(key_id: int, admin: Principal = Depends(require_role(Role.ADMIN)), db: Session = Depends(get_db)):
    from datetime import UTC, datetime

    key = db.get(ApiKey, key_id)
    if key is None:
        raise APIError(404, "not_found", f"API key {key_id} not found")
    key.is_active = False
    key.revoked_at = datetime.now(UTC)
    db.commit()
    clear_api_key_cache()
    log.info("admin.api_key_revoked", key_id=key_id, by=admin.user_id)
    return _key_out(key)
