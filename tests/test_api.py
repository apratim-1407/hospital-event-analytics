"""Integration tests: real Postgres (throwaway database), real Redis (logical DBs 10-13)."""
import uuid

import pytest
from sqlalchemy import text

from app.core.db import SessionLocal

API = "/api/v1"


def _event(**over):
    e = {
        "patient_ref": "p_48213", "event_type": "admission", "department_id": 3,
        "event_time": "2025-03-14T02:40:00Z", "severity": 4, "admission_source": "emergency", "bed_id": "C-212",
    }
    e.update(over)
    return e


# ---------------------------------------------------------------------- auth
def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["database"] and body["redis"]


def test_register_creates_a_viewer_and_rejects_duplicates(client):
    r = client.post(f"{API}/auth/register", json={"email": "New.User@Test.local", "password": "a-long-password-1"})
    assert r.status_code == 201 and r.json()["role"] == "viewer" and r.json()["email"] == "new.user@test.local"
    assert "password" not in r.text
    dup = client.post(f"{API}/auth/register", json={"email": "new.user@test.local", "password": "a-long-password-1"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "conflict"


def test_register_validates_input(client):
    assert client.post(f"{API}/auth/register", json={"email": "not-an-email", "password": "a-long-password-1"}).status_code == 400
    assert client.post(f"{API}/auth/register", json={"email": "x@y.zz", "password": "short"}).status_code == 400


def test_login_failures_are_indistinguishable(client, seeded):
    wrong_pw = client.post(f"{API}/auth/login", json={"email": seeded["viewer"][0], "password": "nope-nope-nope"})
    no_user = client.post(f"{API}/auth/login", json={"email": "ghost@test.local", "password": "nope-nope-nope"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json()["error"]["message"] == no_user.json()["error"]["message"]


def test_error_envelope_carries_the_correlation_id(client):
    cid = str(uuid.uuid4())
    r = client.get(f"{API}/jobs", headers={"X-Correlation-ID": cid})
    assert r.status_code == 401
    assert r.json()["error"]["correlation_id"] == cid and r.headers["x-correlation-id"] == cid


def test_correlation_id_is_generated_when_absent(client):
    r = client.get("/health")
    uuid.UUID(r.headers["x-correlation-id"])


def test_expired_and_tampered_tokens_are_rejected(client, tokens):
    good = tokens["viewer"]["access_token"]
    assert client.get(f"{API}/analytics/admissions", headers={"Authorization": f"Bearer {good}x"}).status_code == 401
    assert client.get(f"{API}/analytics/admissions", headers={"Authorization": f"Bearer {tokens['viewer']['refresh_token']}"}).status_code == 401


def test_refresh_rotates_and_reuse_revokes_the_whole_family(client, seeded):
    email, pw = seeded["analyst"]
    first = client.post(f"{API}/auth/login", json={"email": email, "password": pw}).json()
    second = client.post(f"{API}/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert second.status_code == 200
    # Replaying the spent token is a theft signal: it fails AND kills the new token too.
    replay = client.post(f"{API}/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "token_reuse_detected"
    after = client.post(f"{API}/auth/refresh", json={"refresh_token": second.json()["refresh_token"]})
    assert after.status_code == 401


# ---------------------------------------------------------------------- RBAC
JOB = {"job_type": "admission_patterns", "start_date": "2025-03-01", "end_date": "2025-03-31"}


def test_viewer_cannot_submit_a_job_but_analyst_can(client, auth, monkeypatch):
    from app.pipeline.tasks import run_job

    monkeypatch.setattr(run_job, "apply_async", lambda *a, **k: None)
    denied = client.post(f"{API}/jobs", json=JOB, headers=auth("viewer"))
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "forbidden"
    ok = client.post(f"{API}/jobs", json=JOB, headers=auth("analyst"))
    assert ok.status_code == 202 and ok.json()["status"] == "queued"


def test_only_admin_sees_metrics_and_manages_users(client, auth):
    assert client.get(f"{API}/metrics", headers=auth("analyst")).status_code == 403
    assert client.get(f"{API}/metrics", headers=auth("admin")).status_code == 200
    assert client.get(f"{API}/admin/users", headers=auth("analyst")).status_code == 403
    assert len(client.get(f"{API}/admin/users", headers=auth("admin")).json()) >= 3


def test_every_role_can_read_analytics(client, auth):
    for role in ("viewer", "analyst", "admin"):
        assert client.get(f"{API}/analytics/admissions", headers=auth(role)).status_code == 200


def test_admin_cannot_demote_themselves(client, auth, tokens):
    me = next(u for u in client.get(f"{API}/admin/users", headers=auth("admin")).json() if u["email"] == "admin@test.local")
    r = client.patch(f"{API}/admin/users/{me['user_id']}", json={"role": "viewer"}, headers=auth("admin"))
    assert r.status_code == 409


# -------------------------------------------------------------------- ingest
def test_ingest_requires_an_api_key_not_a_jwt(client, auth):
    assert client.post(f"{API}/events", json=_event(), headers=auth("admin")).status_code == 401
    assert client.post(f"{API}/events", json=_event(), headers={"X-API-Key": "hea_wrong"}).status_code == 401


def test_ingest_stores_the_event_and_threads_the_correlation_id(client, key_header, auth):
    cid = str(uuid.uuid4())
    r = client.post(f"{API}/events", json=_event(patient_ref="p_trace1"), headers={**key_header, "X-Correlation-ID": cid})
    assert r.status_code == 201 and r.json()["correlation_id"] == cid
    rows = client.get(f"{API}/events", params={"patient_ref": "p_trace1"}, headers=auth("viewer")).json()["data"]
    assert len(rows) == 1 and rows[0]["correlation_id"] == cid and rows[0]["event_type"] == "admission"
    with SessionLocal() as db:
        row = db.execute(text("SELECT ingested_at, ingested_by_key FROM raw.hospital_events WHERE correlation_id = :c"), {"c": cid}).one()
        assert row.ingested_at is not None and row.ingested_by_key is not None  # server-stamped, never client-supplied


def test_ingest_validation_is_400_and_semantic_errors_are_422(client, key_header):
    assert client.post(f"{API}/events", json=_event(severity=9), headers=key_header).status_code == 400
    assert client.post(f"{API}/events", json=_event(event_type="nonsense"), headers=key_header).status_code == 400
    assert client.post(f"{API}/events", json=_event(department_id=99), headers=key_header).status_code == 422
    assert client.post(f"{API}/events", json=_event(length_of_stay_hours=5.0), headers=key_header).status_code == 422
    assert client.post(f"{API}/events", json=_event(event_time="2099-01-01T00:00:00Z"), headers=key_header).status_code == 422


def test_raw_events_are_append_only(database):
    with SessionLocal() as db:
        with pytest.raises(Exception, match="append-only"):
            db.execute(text("UPDATE raw.hospital_events SET severity = 1"))
        db.rollback()
        with pytest.raises(Exception, match="append-only"):
            db.execute(text("DELETE FROM raw.hospital_events"))


def test_events_use_cursor_pagination(client, key_header, auth):
    for i in range(5):
        client.post(f"{API}/events", json=_event(patient_ref="p_page", event_time=f"2025-04-01T0{i}:00:00Z"), headers=key_header)
    h = auth("viewer")
    page1 = client.get(f"{API}/events", params={"patient_ref": "p_page", "limit": 2}, headers=h).json()
    assert len(page1["data"]) == 2 and page1["next_cursor"]
    page2 = client.get(f"{API}/events", params={"patient_ref": "p_page", "limit": 2, "cursor": page1["next_cursor"]}, headers=h).json()
    seen = [e["event_id"] for e in page1["data"] + page2["data"]]
    assert len(set(seen)) == 4
    assert client.get(f"{API}/events", params={"cursor": "garbage!!"}, headers=h).status_code == 400


def test_api_key_can_be_created_used_and_revoked(client, auth):
    created = client.post(f"{API}/admin/api-keys", json={"name": "lifecycle"}, headers=auth("admin"))
    assert created.status_code == 201
    key = created.json()["api_key"]
    assert key.startswith("hea_")
    listed = client.get(f"{API}/admin/api-keys", headers=auth("admin")).text
    assert key not in listed and "key_hash" not in listed  # plaintext shown once; hash never exposed
    with SessionLocal() as db:
        stored = db.execute(text("SELECT key_hash FROM auth.api_keys WHERE name = 'lifecycle'")).scalar_one()
        assert stored != key and len(stored) == 64  # only a SHA-256 hash is stored
    assert client.post(f"{API}/events", json=_event(), headers={"X-API-Key": key}).status_code == 201
    assert client.delete(f"{API}/admin/api-keys/{created.json()['key_id']}", headers=auth("admin")).status_code == 200
    assert client.post(f"{API}/events", json=_event(), headers={"X-API-Key": key}).status_code == 401


def test_rate_limit_returns_429(client, auth, monkeypatch):
    from app.core import deps

    monkeypatch.setattr(deps.settings, "rate_limit_enabled", True)
    key = client.post(f"{API}/admin/api-keys", json={"name": "limited", "rate_limit_per_min": 3}, headers=auth("admin")).json()["api_key"]
    codes = [client.post(f"{API}/events", json=_event(), headers={"X-API-Key": key}).status_code for _ in range(8)]
    assert 201 in codes and 429 in codes


# ---------------------------------------------------- job lifecycle + analytics
@pytest.fixture(scope="module")
def loaded(database, tmp_path_factory):
    """A small generated dataset in the database."""
    from datetime import date

    from scripts.generate import generate, write_events_csv, write_reference_csvs
    from scripts.load_csv import load_direct

    gen = generate(12000, date(2025, 1, 1), date(2025, 2, 28), seed=5)
    out = tmp_path_factory.mktemp("data") / "t.csv"
    write_events_csv(gen.events, out)
    write_reference_csvs(gen, out)
    load_direct(out, truncate=True)
    return gen


def _run_inline(job_id: str) -> dict:
    """Execute a job's pipeline in-process: every map partition, then the reducer."""
    from app.models.analytics import AnalysisJob
    from app.pipeline.map_tasks import process_partition
    from app.pipeline.partition import plan_partitions
    from app.pipeline.reduce import job_plan_from_params, run_reduce

    with SessionLocal() as db:
        job = db.get(AnalysisJob, uuid.UUID(job_id))
        plan, depts = job_plan_from_params(job.job_type, dict(job.params))
    for p in plan_partitions(plan, depts):
        process_partition(job_id, p)
    return run_reduce(job_id)


def _submit(client, auth, body, monkeypatch):
    from app.pipeline.tasks import run_job

    monkeypatch.setattr(run_job, "apply_async", lambda *a, **k: None)
    r = client.post(f"{API}/jobs", json=body, headers=auth("analyst"))
    assert r.status_code == 202, r.text
    return r.json()


def test_job_accepts_with_202_and_reports_partition_count(client, auth, loaded, monkeypatch):
    accepted = _submit(client, auth, {"job_type": "admission_patterns", "start_date": "2025-02-01", "end_date": "2025-02-10", "departments": [1, 2, 3]}, monkeypatch)
    assert accepted["partitions_total"] == 30 and accepted["poll_url"].endswith(accepted["job_id"])
    job = client.get(accepted["poll_url"], headers=auth("analyst")).json()
    assert job["status"] == "queued" and job["partitions_completed"] == 0


def test_unknown_department_and_bad_dates_are_rejected(client, auth, loaded):
    h = auth("analyst")
    assert client.post(f"{API}/jobs", json={"job_type": "full_analysis", "start_date": "2025-02-01", "end_date": "2025-02-10", "departments": [99]}, headers=h).status_code == 422
    assert client.post(f"{API}/jobs", json={"job_type": "full_analysis", "start_date": "2025-02-10", "end_date": "2025-02-01"}, headers=h).status_code == 400
    assert client.post(f"{API}/jobs", json={"job_type": "bogus", "start_date": "2025-02-01", "end_date": "2025-02-10"}, headers=h).status_code == 400


def test_full_pipeline_results_match_raw_data_and_carry_job_lineage(client, auth, loaded, monkeypatch):
    accepted = _submit(client, auth, {"job_type": "full_analysis", "start_date": "2025-01-01", "end_date": "2025-02-28"}, monkeypatch)
    outcome = _run_inline(accepted["job_id"])
    assert outcome["status"] == "completed" and outcome["events_processed"] == loaded.count

    job = client.get(accepted["poll_url"], headers=auth("analyst")).json()
    assert job["status"] == "completed" and job["partitions_completed"] == job["partitions_total"] == 59 * 8
    assert job["progress_pct"] == 100.0 and job["events_processed"] == loaded.count and job["duration_seconds"] is not None

    page = client.get(f"{API}/analytics/admissions", params={"department_id": 1, "start_date": "2025-02-01", "end_date": "2025-02-03"}, headers=auth("viewer")).json()
    assert len(page["data"]) == 3 and all(r["job_id"] == accepted["job_id"] for r in page["data"])
    with SessionLocal() as db:
        expected = db.execute(
            text("SELECT count(*) FROM raw.hospital_events WHERE department_id = 1 AND event_type IN ('admission','icu_admit') "
                 "AND event_time >= '2025-02-01' AND event_time < '2025-02-02'")
        ).scalar_one()
    assert page["data"][0]["admission_count"] == expected


def test_rerunning_a_job_overwrites_instead_of_duplicating(client, auth, loaded, monkeypatch):
    body = {"job_type": "admission_patterns", "start_date": "2025-02-01", "end_date": "2025-02-05"}
    for _ in range(2):
        _run_inline(_submit(client, auth, body, monkeypatch)["job_id"])
    with SessionLocal() as db:
        n = db.execute(text("SELECT count(*) FROM analytics.admission_metrics WHERE metric_date BETWEEN '2025-02-01' AND '2025-02-05'")).scalar_one()
        dupes = db.execute(text("SELECT count(*) FROM (SELECT 1 FROM analytics.admission_metrics GROUP BY metric_date, department_id HAVING count(*) > 1) x")).scalar_one()
    assert n == 5 * 8 and dupes == 0


def test_a_redelivered_reduce_is_harmless(client, auth, loaded, monkeypatch):
    from app.pipeline.reduce import run_reduce

    accepted = _submit(client, auth, {"job_type": "admission_patterns", "start_date": "2025-02-01", "end_date": "2025-02-03"}, monkeypatch)
    _run_inline(accepted["job_id"])
    assert run_reduce(accepted["job_id"]) is None  # already final: skipped, nothing rewritten


def test_a_lost_shuffle_marks_the_job_partial_not_wrong(client, auth, loaded, monkeypatch):
    """If Redis loses a partition's data, the job says so instead of reporting a wrong total."""
    from app.core.redis import get_state_redis

    accepted = _submit(client, auth, {"job_type": "admission_patterns", "start_date": "2025-02-01", "end_date": "2025-02-03"}, monkeypatch)
    jid = accepted["job_id"]
    from app.models.analytics import AnalysisJob
    from app.pipeline.map_tasks import process_partition
    from app.pipeline.partition import plan_partitions
    from app.pipeline.reduce import job_plan_from_params, run_reduce

    with SessionLocal() as db:
        job = db.get(AnalysisJob, uuid.UUID(jid))
        plan, depts = job_plan_from_params(job.job_type, dict(job.params))
    parts = plan_partitions(plan, depts)
    for p in parts:
        process_partition(jid, p)
    get_state_redis().delete(f"job:{jid}:key:{parts[0].key}")
    out = run_reduce(jid)
    assert out["status"] == "partial"
    job = client.get(accepted["poll_url"], headers=auth("analyst")).json()
    assert job["status"] == "partial" and job["error_detail"]["failed_partitions"] == 1


def test_cancel_own_job_only(client, auth, loaded, monkeypatch):
    accepted = _submit(client, auth, {"job_type": "admission_patterns", "start_date": "2025-02-01", "end_date": "2025-02-03"}, monkeypatch)
    # another analyst cannot cancel it; an admin can
    email = "analyst2@test.local"
    client.post(f"{API}/auth/register", json={"email": email, "password": "analyst2-password"})
    with SessionLocal() as db:
        db.execute(text("UPDATE auth.users SET role = 'analyst' WHERE email = :e"), {"e": email})
        db.commit()
    tok = client.post(f"{API}/auth/login", json={"email": email, "password": "analyst2-password"}).json()["access_token"]
    assert client.post(f"{accepted['poll_url']}/cancel", headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    r = client.post(f"{accepted['poll_url']}/cancel", headers=auth("admin"))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert client.post(f"{accepted['poll_url']}/cancel", headers=auth("admin")).status_code == 409


def test_rerun_endpoint_creates_a_new_job_with_the_same_parameters(client, auth, loaded, monkeypatch):
    accepted = _submit(client, auth, {"job_type": "admission_patterns", "start_date": "2025-02-01", "end_date": "2025-02-03", "departments": [2]}, monkeypatch)
    again = client.post(f"{accepted['poll_url']}/rerun", headers=auth("analyst"))
    assert again.status_code == 202 and again.json()["job_id"] != accepted["job_id"] and again.json()["partitions_total"] == 3


def test_job_list_filters_and_paginates(client, auth, loaded):
    h = auth("analyst")
    page = client.get(f"{API}/jobs", params={"limit": 2}, headers=h).json()
    assert len(page["data"]) == 2 and page["next_cursor"]
    done = client.get(f"{API}/jobs", params={"status": "completed"}, headers=h).json()["data"]
    assert done and all(j["status"] == "completed" for j in done)


def test_analytics_responses_are_cached_and_invalidated_by_new_results(client, auth, loaded, monkeypatch):
    h = auth("viewer")
    params = {"department_id": 2, "start_date": "2025-02-01", "end_date": "2025-02-02"}
    first = client.get(f"{API}/analytics/admissions", params=params, headers=h)
    second = client.get(f"{API}/analytics/admissions", params=params, headers=h)
    assert first.headers["x-cache"] == "MISS" and second.headers["x-cache"] == "HIT"
    assert first.json() == second.json()
    from app.services.cache import bump_analytics_version

    bump_analytics_version()  # what the reducer does when a job finishes
    assert client.get(f"{API}/analytics/admissions", params=params, headers=h).headers["x-cache"] == "MISS"
    ratio = client.get(f"{API}/metrics", headers=auth("admin")).json()["M4_cache"]
    assert ratio["hits"] >= 1 and ratio["hit_ratio"] is not None


def test_anomalies_endpoint_filters_by_severity_and_validates(client, auth, loaded, monkeypatch):
    _run_inline(_submit(client, auth, {"job_type": "anomaly_detection", "start_date": "2025-01-20", "end_date": "2025-02-28"}, monkeypatch)["job_id"])
    h = auth("viewer")
    allw = client.get(f"{API}/analytics/anomalies", params={"limit": 500}, headers=h).json()["data"]
    crit = client.get(f"{API}/analytics/anomalies", params={"severity": "critical", "limit": 500}, headers=h).json()["data"]
    assert allw and crit and all(a["severity_label"] == "critical" for a in crit) and len(crit) <= len(allw)
    assert all(a["direction"] in ("high", "low") for a in allw)
    assert client.get(f"{API}/analytics/anomalies", params={"severity": "bogus"}, headers=h).status_code == 400


def test_metrics_report_all_four_measures(client, auth, loaded):
    m = client.get(f"{API}/metrics", headers=auth("admin")).json()
    assert {"M1_job_processing_time", "M2_ingestion_throughput", "M3_api_latency_ms", "M4_cache"} <= set(m)
    assert m["M1_job_processing_time"]["by_job_type"]
    lat = next(iter(m["M3_api_latency_ms"].values()))
    assert lat["p50"] <= lat["p95"] <= lat["p99"]


# ---------------------------------------------------------------- failure paths
def test_broker_down_returns_503_and_ingestion_keeps_working(client, auth, key_header, loaded, monkeypatch):
    from kombu.exceptions import OperationalError

    from app.pipeline.tasks import run_job

    def boom(*a, **k):
        raise OperationalError("broker unreachable")

    monkeypatch.setattr(run_job, "apply_async", boom)
    r = client.post(f"{API}/jobs", json=JOB, headers=auth("analyst"))
    assert r.status_code == 503 and r.json()["error"]["code"] == "service_unavailable"
    assert r.headers.get("retry-after")
    with SessionLocal() as db:
        status = db.execute(text("SELECT status FROM analytics.analysis_jobs ORDER BY created_at DESC LIMIT 1")).scalar_one()
    assert status == "failed"  # the failed submission is recorded, not silently dropped
    # The write path never touches the broker: analytics is down, capture is not.
    assert client.post(f"{API}/events", json=_event(patient_ref="p_during_outage"), headers=key_header).status_code == 201


def test_redis_down_does_not_stop_ingestion_or_reads(client, auth, key_header, loaded, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    from app.core import redis as redis_core

    class Dead:
        def __getattr__(self, name):
            raise RedisConnectionError("redis is down")

    monkeypatch.setattr(redis_core, "_clients", {i: Dead() for i in range(4)})
    monkeypatch.setattr(redis_core, "_down_until", 0.0)
    assert client.post(f"{API}/events", json=_event(patient_ref="p_redis_down"), headers=key_header).status_code == 201
    assert client.get(f"{API}/analytics/admissions", headers=auth("viewer")).status_code == 200  # cache bypassed, DB answers


# ------------------------------------------------------------------ bulk load
def _storage_up():
    try:
        from app.services import storage

        storage.ensure_bucket()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _storage_up(), reason="object storage not reachable")
def test_bulk_load_from_object_storage_is_atomic(client, key_header, loaded, tmp_path, monkeypatch):
    from app.pipeline.tasks import load_batch
    from app.services import ingest, storage

    monkeypatch.setattr(load_batch, "apply_async", lambda *a, **k: None)
    good = tmp_path / "good.csv"
    good.write_text(
        "patient_ref,event_type,department_id,event_time,severity,admission_source,bed_id,length_of_stay_hours\n"
        "p_b1,admission,2,2025-05-01T10:00:00Z,3,emergency,G-001,\n"
        "p_b1,discharge,2,2025-05-03T10:00:00Z,3,emergency,G-001,48.00\n"
    )
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "patient_ref,event_type,department_id,event_time,severity,admission_source,bed_id,length_of_stay_hours\n"
        "p_b2,admission,2,2025-05-01T10:00:00Z,3,emergency,G-001,\n"
        "p_b2,not_a_type,2,2025-05-03T10:00:00Z,3,emergency,G-001,\n"
    )
    uri_good = storage.upload_file(str(good), "tests/good.csv")
    uri_bad = storage.upload_file(str(bad), "tests/bad.csv")

    r = client.post(f"{API}/events/batch", json={"source_uri": uri_good}, headers=key_header)
    assert r.status_code == 202
    ingest.run_batch_load(r.json()["batch_id"], uri_good, str(uuid.uuid4()), None)
    status = client.get(r.json()["poll_url"], headers=key_header).json()
    assert status["status"] == "completed" and status["row_count"] == 2

    r2 = client.post(f"{API}/events/batch", json={"source_uri": uri_bad}, headers=key_header)
    ingest.run_batch_load(r2.json()["batch_id"], uri_bad, str(uuid.uuid4()), None)
    failed = client.get(r2.json()["poll_url"], headers=key_header).json()
    assert failed["status"] == "failed" and "not_a_type" in failed["error_detail"]["message"]
    with SessionLocal() as db:
        assert db.execute(text("SELECT count(*) FROM raw.hospital_events WHERE patient_ref = 'p_b2'")).scalar_one() == 0  # all or nothing


@pytest.mark.skipif(not _storage_up(), reason="object storage not reachable")
def test_a_redelivered_bulk_load_does_not_insert_twice(client, key_header, loaded, tmp_path, monkeypatch):
    """The broker redelivers any task still running after the visibility timeout. Bulk load is not
    idempotent, so only the first delivery may claim the batch."""
    from app.pipeline.tasks import load_batch
    from app.services import ingest, storage

    monkeypatch.setattr(load_batch, "apply_async", lambda *a, **k: None)
    f = tmp_path / "dup.csv"
    f.write_text(
        "patient_ref,event_type,department_id,event_time,severity,admission_source,bed_id,length_of_stay_hours\n"
        "p_dup,admission,2,2025-06-01T10:00:00Z,3,emergency,G-001,\n"
    )
    uri = storage.upload_file(str(f), "tests/dup.csv")
    batch = client.post(f"{API}/events/batch", json={"source_uri": uri}, headers=key_header).json()["batch_id"]
    assert ingest.run_batch_load(batch, uri, str(uuid.uuid4()), None) == 1
    assert ingest.run_batch_load(batch, uri, str(uuid.uuid4()), None) == 0  # duplicate delivery: skipped
    with SessionLocal() as db:
        assert db.execute(text("SELECT count(*) FROM raw.hospital_events WHERE patient_ref = 'p_dup'")).scalar_one() == 1


def test_batch_rejects_foreign_buckets_and_missing_objects(client, key_header):
    assert client.post(f"{API}/events/batch", json={"source_uri": "s3://some-other-bucket/x.csv"}, headers=key_header).status_code == 422
    assert client.post(f"{API}/events/batch", json={"source_uri": "http://evil.example/x.csv"}, headers=key_header).status_code == 422


# ------------------------------------------------------------------ web UI + overview
def test_ui_is_served_and_root_redirects(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (301, 302, 307) and r.headers["location"] == "/app/"
    page = client.get("/app/")
    assert page.status_code == 200 and "Hospital Activity" in page.text
    assert client.get("/app/app.js").status_code == 200 and client.get("/app/styles.css").status_code == 200


def test_overview_endpoints_summarise_the_analytics_tables(client, auth, loaded, monkeypatch):
    _run_inline(_submit(client, auth, {"job_type": "full_analysis", "start_date": "2025-01-01", "end_date": "2025-02-28"}, monkeypatch)["job_id"])
    h = auth("viewer")
    d = client.get(f"{API}/overview/departments", headers=h).json()
    assert len(d["departments"]) == 8 and d["analysed_range"]["first"] == "2025-01-01"

    s = client.get(f"{API}/overview/summary", params={"start_date": "2025-01-01", "end_date": "2025-02-28"}, headers=h).json()
    with SessionLocal() as db:
        direct = db.execute(text("SELECT sum(admission_count) FROM analytics.admission_metrics")).scalar_one()
    assert s["totals"]["admissions"] == direct and s["granularity"] == "day"  # 59 days: daily points
    assert sum(x["admissions"] for x in s["departments"]) == direct
    assert s["busiest_department"] and s["busiest_day"]["admissions"] > 0

    one = client.get(f"{API}/overview/summary", params={"start_date": "2025-01-01", "end_date": "2025-01-31", "department_id": 1}, headers=h).json()
    assert [x["department_id"] for x in one["departments"]] == [1] and one["granularity"] == "day" and len(one["trend"]) == 31

    p = client.get(f"{API}/overview/weekly-pattern", params={"start_date": "2025-01-22", "end_date": "2025-02-28"}, headers=h).json()
    assert len(p["weekday"]) == len(p["weekend"]) == 24 and all(0 <= v <= 2 for v in p["weekday"] if v is not None)


def test_overview_requires_a_login_and_validates_dates(client, auth):
    assert client.get(f"{API}/overview/departments").status_code == 401
    bad = client.get(f"{API}/overview/summary", params={"start_date": "2025-03-01", "end_date": "2025-01-01"}, headers=auth("viewer"))
    assert bad.status_code == 400
