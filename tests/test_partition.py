from datetime import date

from app.pipeline.partition import (
    LOOKBACK_DAYS,
    Partition,
    make_plan,
    parse_partition_key,
    plan_partitions,
)

DS, DE = date(2023, 1, 1), date(2025, 12, 31)


def test_march_across_three_departments_is_93_partitions():
    plan = make_plan("admission_patterns", date(2025, 3, 1), date(2025, 3, 31), DS, DE)
    assert len(plan_partitions(plan, [1, 2, 3])) == 93


def test_eight_departments_one_month_is_248():
    plan = make_plan("admission_patterns", date(2025, 3, 1), date(2025, 3, 31), DS, DE)
    assert len(plan_partitions(plan, list(range(1, 9)))) == 248


def test_six_months_across_eight_departments():
    plan = make_plan("admission_patterns", date(2025, 1, 1), date(2025, 6, 30), DS, DE)
    assert len(plan_partitions(plan, list(range(1, 9)))) == 181 * 8


def test_anomaly_jobs_read_a_lookback_but_report_only_the_window():
    plan = make_plan("anomaly_detection", date(2025, 3, 1), date(2025, 3, 31), DS, DE)
    assert (plan.start - plan.range_start).days == LOOKBACK_DAYS
    assert plan.range_end == date(2025, 3, 31)
    plain = make_plan("admission_patterns", date(2025, 3, 1), date(2025, 3, 31), DS, DE)
    assert plain.range_start == date(2025, 3, 1)


def test_range_is_clamped_to_where_data_exists():
    plan = make_plan("full_analysis", date(2020, 1, 1), date(2030, 1, 1), DS, DE)
    assert plan.range_start == DS and plan.range_end == DE


def test_lookback_cannot_precede_the_first_event():
    plan = make_plan("full_analysis", date(2023, 1, 10), date(2023, 2, 10), DS, DE)
    assert plan.range_start == DS


def test_window_entirely_outside_the_data_is_empty():
    plan = make_plan("admission_patterns", date(2030, 1, 1), date(2030, 2, 1), DS, DE)
    assert plan.empty and plan_partitions(plan, [1, 2]) == []


def test_no_data_at_all_is_empty():
    assert make_plan("full_analysis", date(2025, 1, 1), date(2025, 1, 31), None, None).empty


def test_partition_key_round_trips_and_bounds_are_one_utc_day():
    p = Partition(date(2025, 3, 14), 3)
    assert parse_partition_key(p.key) == p
    t0, t1 = p.bounds
    assert (t1 - t0).total_seconds() == 86400 and t0.tzinfo is not None


def test_provider_style_database_urls_are_normalised():
    from app.core.config import Settings

    for raw in ("postgres://u:p@h:5432/d", "postgresql://u:p@h:5432/d", "postgresql+psycopg2://u:p@h:5432/d"):
        assert Settings(database_url=raw).database_url == "postgresql+psycopg2://u:p@h:5432/d"
