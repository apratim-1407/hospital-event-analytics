from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.security import MAX_PASSWORD_BYTES, Role

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterRequest(BaseModel):
    email: str = Field(pattern=_EMAIL_PATTERN, max_length=255, examples=["analyst@hospital.example"])
    password: str = Field(min_length=10, max_length=72)

    @field_validator("password")
    @classmethod
    def _bcrypt_limit(cls, v: str) -> str:
        if len(v.encode()) > MAX_PASSWORD_BYTES:
            raise ValueError("password must be at most 72 bytes")
        return v

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class LoginRequest(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=72)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(BaseModel):
    user_id: int
    email: str
    role: str
    is_active: bool
    created_at: datetime


class UserUpdate(BaseModel):
    role: Role | None = None
    is_active: bool | None = None


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    rate_limit_per_min: int = Field(default=6000, ge=1, le=1_000_000)


class ApiKeyOut(BaseModel):
    key_id: int
    name: str
    key_prefix: str
    scope: str
    rate_limit_per_min: int
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreated(ApiKeyOut):
    api_key: str = Field(description="Shown once. Only its SHA-256 hash is stored.")
