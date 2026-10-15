from datetime import date, datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class JobType(str, Enum):
    ADMISSION_PATTERNS = "admission_patterns"
    RESOURCE_UTILISATION = "resource_utilisation"
    ANOMALY_DETECTION = "anomaly_detection"
    FULL_ANALYSIS = "full_analysis"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


class JobCreate(BaseModel):
    job_type: JobType = Field(examples=["admission_patterns"])
    start_date: date = Field(examples=["2025-03-01"])
    end_date: date = Field(examples=["2025-03-31"])
    departments: list[int] | None = Field(
        default=None, description="Department IDs to analyse; omit for all", examples=[[1, 2, 3]]
    )
    trace_correlation_ids: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Event correlation IDs to follow end-to-end through the map and reduce logs",
    )

    @model_validator(mode="after")
    def _ordered(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class JobOut(BaseModel):
    job_id: UUID
    job_type: str
    status: str
    params: dict
    requested_by: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None
    events_processed: int | None
    partitions_total: int
    partitions_completed: int
    progress_pct: float
    error_detail: dict | None
    poll_url: str


class JobAccepted(BaseModel):
    job_id: UUID
    status: str
    partitions_total: int
    poll_url: str


class JobPage(BaseModel):
    data: list[JobOut]
    next_cursor: str | None
