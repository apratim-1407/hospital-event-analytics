"""analytics.* : derived, rewritable, small, read-hot.

Every row carries the job_id that produced it, so any number returned by the API
traces back to the run, its parameters and its timing.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

JOB_STATUSES = ("queued", "running", "completed", "failed", "partial", "cancelled")
JOB_TYPES = ("admission_patterns", "resource_utilisation", "anomaly_detection", "full_analysis")


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    __table_args__ = {"schema": "analytics"}

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    requested_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    events_processed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    partitions_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    partitions_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class AdmissionMetric(Base):
    """Grain: one row per (date, department)."""

    __tablename__ = "admission_metrics"
    __table_args__ = {"schema": "analytics"}

    metric_date: Mapped[date] = mapped_column(Date, primary_key=True)
    department_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admission_count: Mapped[int] = mapped_column(Integer, nullable=False)
    discharge_count: Mapped[int] = mapped_column(Integer, nullable=False)
    transfer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_severity: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    avg_length_of_stay_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    peak_hour: Mapped[int | None] = mapped_column(SmallInteger)
    source_breakdown: Mapped[dict] = mapped_column(JSONB, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analytics.analysis_jobs.job_id"), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResourceMetric(Base):
    """Grain: one row per (date, hour, department). Hourly on purpose: a ward can
    run at 100% all morning and 40% all night, so a daily average hides the story."""

    __tablename__ = "resource_metrics"
    __table_args__ = {"schema": "analytics"}

    metric_date: Mapped[date] = mapped_column(Date, primary_key=True)
    metric_hour: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    department_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occupied_beds: Mapped[int] = mapped_column(Integer, nullable=False)
    bed_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    occupancy_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    icu_occupancy_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    staff_to_patient_ratio: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analytics.analysis_jobs.job_id"), nullable=False)


class AnomalyWindow(Base):
    __tablename__ = "anomaly_windows"
    __table_args__ = {"schema": "analytics"}

    anomaly_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    department_id: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_name: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_value: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    expected_value: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    z_score: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    severity_label: Mapped[str] = mapped_column(String(12), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analytics.analysis_jobs.job_id"), nullable=False)
