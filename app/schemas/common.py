import base64
import json

from pydantic import BaseModel

from app.core.errors import APIError


class ErrorBody(BaseModel):
    code: str
    message: str
    correlation_id: str | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


def encode_cursor(values: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(values, default=str).encode()).decode()


def decode_cursor(cursor: str | None) -> dict | None:
    """Cursor pagination everywhere: keyset, never OFFSET, because OFFSET degrades badly
    at a million rows."""
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except Exception:
        raise APIError(400, "invalid_cursor", "The pagination cursor is malformed")
