from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel


class AdmissionRow(BaseModel):
    metric_date: date
    department_id: int
    admission_count: int
    discharge_count: int
    transfer_count: int
    avg_severity: float | None
    avg_length_of_stay_hours: float | None
    peak_hour: int | None
    source_breakdown: dict
    job_id: UUID


class AdmissionPage(BaseModel):
    data: list[AdmissionRow]
    next_cursor: str | None


class ResourceRow(BaseModel):
    metric_date: date
    metric_hour: int
    department_id: int
    occupied_beds: int
    bed_capacity: int
    occupancy_rate: float
    icu_occupancy_rate: float | None
    staff_to_patient_ratio: float
    job_id: UUID


class ResourcePage(BaseModel):
    data: list[ResourceRow]
    next_cursor: str | None


class AnomalyRow(BaseModel):
    anomaly_id: int
    window_start: datetime
    window_end: datetime
    department_id: int
    metric_name: str
    observed_value: float
    expected_value: float
    z_score: float
    severity_label: str
    direction: str
    job_id: UUID


class AnomalyPage(BaseModel):
    data: list[AnomalyRow]
    next_cursor: str | None
