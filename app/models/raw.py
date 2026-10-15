"""raw.* : the immutable source of truth.

raw.hospital_events is append-only and partitioned by month. The DDL (partitions,
enums, the immutability trigger) lives in the Alembic migration; the Table object
here exists so queries can be written against column objects.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = {"schema": "raw"}

    department_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    dept_type: Mapped[str] = mapped_column(String(16), nullable=False)
    bed_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    staff_count: Mapped[int] = mapped_column(Integer, nullable=False)


class Bed(Base):
    __tablename__ = "beds"
    __table_args__ = {"schema": "raw"}

    bed_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("raw.departments.department_id"), nullable=False)
    bed_type: Mapped[str] = mapped_column(String(16), nullable=False)


class IngestBatch(Base):
    __tablename__ = "ingest_batches"
    __table_args__ = {"schema": "raw"}

    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    error_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


hospital_events = Table(
    "hospital_events",
    Base.metadata,
    Column("event_id", BigInteger, primary_key=True),
    Column("patient_ref", String(32), nullable=False),
    Column("event_type", String, nullable=False),  # raw.event_type enum
    Column("department_id", Integer, nullable=False),
    Column("event_time", DateTime(timezone=True), primary_key=True, nullable=False),
    Column("severity", SmallInteger, nullable=False),
    Column("admission_source", String, nullable=False),  # raw.admission_source enum
    Column("bed_id", String(16)),
    Column("length_of_stay_hours", Numeric(8, 2)),
    Column("ingested_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("correlation_id", UUID(as_uuid=True), nullable=False),
    Column("batch_id", UUID(as_uuid=True)),
    Column("ingested_by_key", Integer),
    schema="raw",
)

# Event semantics shared by the API validator, the map task and the generator.
ARRIVAL_ADMISSION_TYPES = ("admission", "icu_admit")  # counted as admissions
ARRIVAL_TRANSFER_TYPES = ("transfer",)                # transfer into the department
DEPARTURE_TYPES = ("discharge", "icu_discharge")
OCCUPANCY_UP = ARRIVAL_ADMISSION_TYPES + ARRIVAL_TRANSFER_TYPES  # +1 bed
OCCUPANCY_DOWN = DEPARTURE_TYPES                                   # -1 bed
EVENT_TYPES = ("admission", "discharge", "transfer", "icu_admit", "icu_discharge", "surgery")
ADMISSION_SOURCES = ("emergency", "referral", "transfer", "scheduled")
