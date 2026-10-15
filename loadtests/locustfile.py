"""Locust scenario: a weighted mix, not one endpoint hammered, because a realistic mix is
what reveals interaction effects (reads, writes and job submission contending for the same
database).

    50%  read analytics results
    25%  ingest a single event          (X-API-Key)
    15%  poll job status
     7%  query raw events with filters
     3%  submit a new (small) job

Run (via the tools container, headless):
    docker compose --profile tools run --rm tools locust -f loadtests/locustfile.py \\
        --host http://api:8000 --headless -u 100 -r 10 -t 5m \\
        --html benchmarks/locust_100.html --csv benchmarks/locust_100
"""
import os
import random
import uuid
from datetime import UTC, date, datetime, timedelta

from locust import HttpUser, between, events, task

ANALYST_EMAIL = os.environ.get("SEED_ANALYST_EMAIL", "analyst@hospital.local")
ANALYST_PASSWORD = os.environ.get("SEED_ANALYST_PASSWORD", "Analyst#12345")
API_KEY = os.environ.get("SEED_INGEST_API_KEY", "hea_dev_ingest_key_change_me")
# Dates to query: the large dataset covers 2023-2025, the small one 2025-03.
DATA_FIRST = date.fromisoformat(os.environ.get("LOAD_DATA_START", "2025-03-01"))
DATA_DAYS = int(os.environ.get("LOAD_DATA_DAYS", "31"))

EVENT_TYPES = ["admission", "discharge", "transfer", "icu_admit", "icu_discharge", "surgery"]
SOURCES = ["emergency", "referral", "transfer", "scheduled"]


def _random_day() -> date:
    return DATA_FIRST + timedelta(days=random.randrange(DATA_DAYS))


class HospitalUser(HttpUser):
    wait_time = between(0.5, 2)

    def on_start(self):
        self.job_ids: list[str] = []
        self._login()
        r = self.client.get("/api/v1/jobs?limit=20", headers=self.auth, name="GET /api/v1/jobs (setup)")
        if r.ok:
            self.job_ids = [j["job_id"] for j in r.json().get("data", [])]

    def _login(self):
        r = self.client.post(
            "/api/v1/auth/login", json={"email": ANALYST_EMAIL, "password": ANALYST_PASSWORD}, name="POST /api/v1/auth/login"
        )
        token = r.json().get("access_token") if r.ok else None
        self.auth = {"Authorization": f"Bearer {token}"} if token else {}

    def _get(self, path, name, **kw):
        with self.client.get(path, headers=self.auth, name=name, catch_response=True, **kw) as r:
            if r.status_code == 401:  # token expired during a long run
                self._login()
                r.failure("401 (re-authenticated)")
            elif r.status_code >= 400:
                r.failure(f"{r.status_code}")
            return r

    @task(50)
    def read_analytics(self):
        d = _random_day()
        kind = random.choice(["admissions", "resources", "anomalies"])
        dept = random.randint(1, 8)
        if kind == "admissions":
            self._get(f"/api/v1/analytics/admissions?start_date={d}&end_date={d + timedelta(days=6)}&department_id={dept}",
                      "GET /api/v1/analytics/admissions")
        elif kind == "resources":
            self._get(f"/api/v1/analytics/resources?start_date={d}&end_date={d}&department_id={dept}",
                      "GET /api/v1/analytics/resources")
        else:
            self._get(f"/api/v1/analytics/anomalies?severity=high&severity=critical&limit=50", "GET /api/v1/analytics/anomalies")

    @task(25)
    def ingest_event(self):
        # Live events ("a few minutes ago"), not backfill: they land in the current month's partition
        # and never contaminate the 2023-2025 benchmark dataset whose results are being queried.
        when = datetime.now(UTC) - timedelta(seconds=random.randint(0, 3600))
        etype = random.choice(EVENT_TYPES)
        body = {
            "patient_ref": f"p_load_{random.randint(1, 50000)}",
            "event_type": etype,
            "department_id": random.randint(1, 8),
            "event_time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "severity": random.randint(1, 5),
            "admission_source": random.choice(SOURCES),
            "bed_id": f"G-{random.randint(1, 99):03d}",
        }
        if etype in ("discharge", "icu_discharge"):
            body["length_of_stay_hours"] = round(random.uniform(2, 120), 2)
        with self.client.post(
            "/api/v1/events", json=body, headers={"X-API-Key": API_KEY, "X-Correlation-ID": str(uuid.uuid4())},
            name="POST /api/v1/events", catch_response=True,
        ) as r:
            if r.status_code == 429:
                r.failure("429 rate limited")
            elif r.status_code >= 400:
                r.failure(f"{r.status_code}")

    @task(15)
    def poll_job(self):
        if not self.job_ids:
            return
        self._get(f"/api/v1/jobs/{random.choice(self.job_ids)}", "GET /api/v1/jobs/{id}")

    @task(7)
    def query_raw_events(self):
        d = _random_day()
        self._get(
            f"/api/v1/events?start={d}T00:00:00Z&end={d + timedelta(days=1)}T00:00:00Z&department_id={random.randint(1, 8)}&limit=50",
            "GET /api/v1/events",
        )

    @task(3)
    def submit_job(self):
        d = _random_day()
        with self.client.post(
            "/api/v1/jobs",
            json={"job_type": "admission_patterns", "start_date": str(d), "end_date": str(d + timedelta(days=2)), "departments": [random.randint(1, 8)]},
            headers=self.auth, name="POST /api/v1/jobs", catch_response=True,
        ) as r:
            if r.status_code == 202:
                self.job_ids.append(r.json()["job_id"])
                self.job_ids = self.job_ids[-50:]
            elif r.status_code >= 400:
                r.failure(f"{r.status_code}")


@events.test_start.add_listener
def _banner(environment, **_):
    print(f"[locust] data window {DATA_FIRST} + {DATA_DAYS}d, ingest key {API_KEY[:8]}..., analyst {ANALYST_EMAIL}")
