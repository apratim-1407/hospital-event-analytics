"""Measured results (benchmarks, load tests, the failure experiment) for the Reports page.

These are files produced by scripts/benchmark.py, scripts/loadtest.py and scripts/demo.py. If they
are not present (a fresh clone, or an image built without them) the endpoint says so instead of
inventing numbers."""
import json
from pathlib import Path

from fastapi import APIRouter, Depends

from app.core.deps import Principal, require_role
from app.core.security import Role

router = APIRouter(prefix="/reports", tags=["reports"])

BENCHMARK_DIR = Path(__file__).resolve().parents[3] / "benchmarks"


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@router.get("/benchmarks", summary="Recorded benchmark, load-test and failure-experiment results (any role)")
def benchmarks(_: Principal = Depends(require_role(Role.VIEWER))):
    processing = _load(BENCHMARK_DIR / "results.json")
    load_test = _load(BENCHMARK_DIR / "loadtest_results.json")
    failure = None
    evidence = BENCHMARK_DIR / "failure_evidence"
    if evidence.is_dir():
        runs = sorted(d for d in evidence.iterdir() if d.is_dir())
        if runs:
            failure = _load(runs[-1] / "summary.json")
            if failure is not None:
                failure["recorded_at"] = runs[-1].name
    return {
        "available": any(x is not None for x in (processing, load_test, failure)),
        "processing": processing,
        "load_test": load_test,
        "failure_experiment": failure,
    }
