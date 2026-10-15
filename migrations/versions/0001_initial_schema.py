"""initial schema: raw (append-only), analytics (derived), auth

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Months covered by explicit partitions; anything outside lands in the default partition.
PARTITION_FIRST_YEAR = 2022
PARTITION_LAST_YEAR = 2028

DEFAULT_DEPARTMENTS = [
    (1, "Emergency", "emergency", 50, 60),
    (2, "General Medicine", "ward", 120, 90),
    (3, "Cardiology", "ward", 40, 40),
    (4, "Orthopaedics", "ward", 40, 35),
    (5, "Paediatrics", "ward", 35, 35),
    (6, "Maternity", "ward", 30, 30),
    (7, "Surgery", "surgical", 45, 50),
    (8, "ICU", "icu", 20, 45),
]


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS raw")
    op.execute("CREATE SCHEMA IF NOT EXISTS analytics")
    op.execute("CREATE SCHEMA IF NOT EXISTS auth")

    # ---------------------------------------------------------------- auth
    op.execute(
        """
        CREATE TABLE auth.users (
            user_id        SERIAL PRIMARY KEY,
            email          VARCHAR(255) NOT NULL UNIQUE,
            password_hash  TEXT NOT NULL,
            role           VARCHAR(16) NOT NULL DEFAULT 'viewer'
                           CHECK (role IN ('viewer', 'analyst', 'admin')),
            is_active      BOOLEAN NOT NULL DEFAULT TRUE,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE auth.api_keys (
            key_id              SERIAL PRIMARY KEY,
            name                VARCHAR(64) NOT NULL,
            key_prefix          VARCHAR(12) NOT NULL,
            key_hash            CHAR(64) NOT NULL UNIQUE,
            scope               VARCHAR(32) NOT NULL DEFAULT 'ingest',
            rate_limit_per_min  INT NOT NULL DEFAULT 6000 CHECK (rate_limit_per_min > 0),
            is_active           BOOLEAN NOT NULL DEFAULT TRUE,
            created_by          INT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at        TIMESTAMPTZ,
            revoked_at          TIMESTAMPTZ
        )
        """
    )

    # ----------------------------------------------------------------- raw
    op.execute("CREATE TYPE raw.event_type AS ENUM ('admission','discharge','transfer','icu_admit','icu_discharge','surgery')")
    op.execute("CREATE TYPE raw.admission_source AS ENUM ('emergency','referral','transfer','scheduled')")

    op.execute(
        """
        CREATE TABLE raw.departments (
            department_id  INT PRIMARY KEY,
            name           VARCHAR(64) NOT NULL UNIQUE,
            dept_type      VARCHAR(16) NOT NULL,
            bed_capacity   INT NOT NULL CHECK (bed_capacity > 0),
            staff_count    INT NOT NULL CHECK (staff_count > 0)
        )
        """
    )
    for dept in DEFAULT_DEPARTMENTS:
        op.execute(
            "INSERT INTO raw.departments (department_id, name, dept_type, bed_capacity, staff_count) "
            f"VALUES ({dept[0]}, '{dept[1]}', '{dept[2]}', {dept[3]}, {dept[4]})"
        )

    op.execute(
        """
        CREATE TABLE raw.beds (
            bed_id         VARCHAR(16) PRIMARY KEY,
            department_id  INT NOT NULL REFERENCES raw.departments (department_id),
            bed_type       VARCHAR(16) NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX idx_beds_department ON raw.beds (department_id)")

    op.execute(
        """
        CREATE TABLE raw.ingest_batches (
            batch_id        UUID PRIMARY KEY,
            source_uri      TEXT NOT NULL,
            row_count       BIGINT,
            uploaded_by     VARCHAR(64) NOT NULL,
            status          VARCHAR(16) NOT NULL DEFAULT 'queued'
                            CHECK (status IN ('queued', 'loading', 'completed', 'failed')),
            error_detail    JSONB,
            correlation_id  UUID,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at      TIMESTAMPTZ,
            finished_at     TIMESTAMPTZ
        )
        """
    )

    # The central fact table: append-only, partitioned by month on event_time.
    # The partition key must be part of the primary key.
    op.execute(
        """
        CREATE TABLE raw.hospital_events (
            event_id              BIGSERIAL,
            patient_ref           VARCHAR(32) NOT NULL,
            event_type            raw.event_type NOT NULL,
            department_id         INT NOT NULL REFERENCES raw.departments (department_id),
            event_time            TIMESTAMPTZ NOT NULL,
            severity              SMALLINT NOT NULL CHECK (severity BETWEEN 1 AND 5),
            admission_source      raw.admission_source NOT NULL,
            bed_id                VARCHAR(16),
            length_of_stay_hours  NUMERIC(8, 2) CHECK (length_of_stay_hours IS NULL OR length_of_stay_hours >= 0),
            ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            correlation_id        UUID NOT NULL,
            batch_id              UUID,
            ingested_by_key       INT,
            PRIMARY KEY (event_id, event_time)
        ) PARTITION BY RANGE (event_time)
        """
    )
    for year in range(PARTITION_FIRST_YEAR, PARTITION_LAST_YEAR + 1):
        for month in range(1, 13):
            ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
            op.execute(
                f"CREATE TABLE raw.hospital_events_{year}_{month:02d} PARTITION OF raw.hospital_events "
                f"FOR VALUES FROM ('{year}-{month:02d}-01 00:00:00+00') TO ('{ny}-{nm:02d}-01 00:00:00+00')"
            )
    op.execute("CREATE TABLE raw.hospital_events_default PARTITION OF raw.hospital_events DEFAULT")

    # Map tasks scan one (department, day) slice; paged reads scan by time.
    op.execute("CREATE INDEX idx_events_dept_time ON raw.hospital_events (department_id, event_time)")
    op.execute("CREATE INDEX idx_events_time ON raw.hospital_events (event_time, event_id)")
    op.execute("CREATE INDEX idx_events_correlation ON raw.hospital_events (correlation_id)")

    # Immutability: raw events are a log. Corrections are new events, never edits.
    op.execute(
        """
        CREATE FUNCTION raw.forbid_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'raw.hospital_events is append-only: % is not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_events_append_only BEFORE UPDATE OR DELETE ON raw.hospital_events "
        "FOR EACH ROW EXECUTE FUNCTION raw.forbid_mutation()"
    )

    # ----------------------------------------------------------- analytics
    op.execute(
        """
        CREATE TABLE analytics.analysis_jobs (
            job_id                UUID PRIMARY KEY,
            job_type              VARCHAR(32) NOT NULL
                                  CHECK (job_type IN ('admission_patterns', 'resource_utilisation',
                                                      'anomaly_detection', 'full_analysis')),
            status                VARCHAR(16) NOT NULL DEFAULT 'queued'
                                  CHECK (status IN ('queued', 'running', 'completed', 'failed', 'partial', 'cancelled')),
            params                JSONB NOT NULL,
            requested_by          INT,
            correlation_id        UUID,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at            TIMESTAMPTZ,
            finished_at           TIMESTAMPTZ,
            duration_seconds      NUMERIC(10, 3),
            events_processed      BIGINT,
            partitions_total      INT NOT NULL DEFAULT 0,
            partitions_completed  INT NOT NULL DEFAULT 0,
            error_detail          JSONB
        )
        """
    )
    op.execute("CREATE INDEX idx_jobs_status ON analytics.analysis_jobs (status)")
    op.execute("CREATE INDEX idx_jobs_created ON analytics.analysis_jobs (created_at DESC)")

    # Composite primary keys make reducer writes idempotent (INSERT .. ON CONFLICT
    # DO UPDATE), so re-running or re-delivering a job overwrites instead of duplicating.
    op.execute(
        """
        CREATE TABLE analytics.admission_metrics (
            metric_date                DATE NOT NULL,
            department_id              INT NOT NULL REFERENCES raw.departments (department_id),
            admission_count            INT NOT NULL,
            discharge_count            INT NOT NULL,
            transfer_count             INT NOT NULL,
            avg_severity               NUMERIC(3, 2),
            avg_length_of_stay_hours   NUMERIC(6, 2),
            peak_hour                  SMALLINT,
            source_breakdown           JSONB NOT NULL,
            job_id                     UUID NOT NULL REFERENCES analytics.analysis_jobs (job_id),
            computed_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (metric_date, department_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE analytics.resource_metrics (
            metric_date             DATE NOT NULL,
            metric_hour             SMALLINT NOT NULL CHECK (metric_hour BETWEEN 0 AND 23),
            department_id           INT NOT NULL REFERENCES raw.departments (department_id),
            occupied_beds           INT NOT NULL,
            bed_capacity            INT NOT NULL,
            occupancy_rate          NUMERIC(6, 4) NOT NULL,
            icu_occupancy_rate      NUMERIC(6, 4),
            staff_to_patient_ratio  NUMERIC(8, 3) NOT NULL,
            job_id                  UUID NOT NULL REFERENCES analytics.analysis_jobs (job_id),
            PRIMARY KEY (metric_date, metric_hour, department_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE analytics.anomaly_windows (
            anomaly_id      BIGSERIAL PRIMARY KEY,
            window_start    TIMESTAMPTZ NOT NULL,
            window_end      TIMESTAMPTZ NOT NULL,
            department_id   INT NOT NULL REFERENCES raw.departments (department_id),
            metric_name     VARCHAR(32) NOT NULL,
            observed_value  NUMERIC(12, 4) NOT NULL,
            expected_value  NUMERIC(12, 4) NOT NULL,
            z_score         NUMERIC(8, 3) NOT NULL,
            severity_label  VARCHAR(12) NOT NULL
                            CHECK (severity_label IN ('moderate', 'high', 'critical')),
            job_id          UUID NOT NULL REFERENCES analytics.analysis_jobs (job_id),
            UNIQUE (department_id, metric_name, window_start)
        )
        """
    )
    op.execute("CREATE INDEX idx_anomaly_start ON analytics.anomaly_windows (window_start, anomaly_id)")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS analytics CASCADE")
    op.execute("DROP SCHEMA IF EXISTS raw CASCADE")
    op.execute("DROP SCHEMA IF EXISTS auth CASCADE")
