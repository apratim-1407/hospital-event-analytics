from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class EventType(str, Enum):
    ADMISSION = "admission"
    DISCHARGE = "discharge"
    TRANSFER = "transfer"
    ICU_ADMIT = "icu_admit"
    ICU_DISCHARGE = "icu_discharge"
    SURGERY = "surgery"


class AdmissionSource(str, Enum):
    EMERGENCY = "emergency"
    REFERRAL = "referral"
    TRANSFER = "transfer"
    SCHEDULED = "scheduled"


class EventIn(BaseModel):
    """One hospital event. Patients are opaque pseudonymous references, never identities."""

    patient_ref: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$", examples=["p_48213"])
    event_type: EventType
    department_id: int = Field(ge=1, examples=[3])
    event_time: datetime = Field(examples=["2025-03-14T02:40:00Z"])
    severity: int = Field(ge=1, le=5, description="Triage score, 1 (low) to 5 (critical)")
    admission_source: AdmissionSource
    bed_id: str | None = Field(default=None, max_length=16, examples=["C-212"])
    length_of_stay_hours: float | None = Field(
        default=None, ge=0, le=24 * 365, description="Populated on discharge events only"
    )

    @field_validator("event_time")
    @classmethod
    def _assume_utc(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)


class EventAccepted(BaseModel):
    event_id: int
    correlation_id: UUID
    ingested_at: datetime


class EventOut(BaseModel):
    event_id: int
    patient_ref: str
    event_type: str
    department_id: int
    event_time: datetime
    severity: int
    admission_source: str
    bed_id: str | None
    length_of_stay_hours: float | None
    ingested_at: datetime
    correlation_id: UUID
    batch_id: UUID | None


class EventPage(BaseModel):
    data: list[EventOut]
    next_cursor: str | None


class BatchRequest(BaseModel):
    source_uri: str = Field(
        examples=["s3://hospital-events/uploads/march.csv"],
        description="Object in the configured bucket. CSV with header: "
        "patient_ref,event_type,department_id,event_time,severity,admission_source,bed_id,length_of_stay_hours",
    )


class BatchAccepted(BaseModel):
    batch_id: UUID
    status: str
    poll_url: str
