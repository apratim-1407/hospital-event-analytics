import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import Principal, require_role
from app.core.errors import APIError
from app.core.security import Role
from app.schemas.common import ErrorEnvelope, decode_cursor, encode_cursor
from app.schemas.jobs import JobAccepted, JobCreate, JobOut, JobPage, JobStatus
from app.services import jobs as svc

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _cid(request: Request) -> str:
    return request.scope["state"]["correlation_id"]


def _accepted(job) -> JobAccepted:
    return JobAccepted(
        job_id=job.job_id,
        status=job.status,
        partitions_total=job.partitions_total,
        poll_url=f"/api/v1/jobs/{job.job_id}",
    )


@router.post(
    "",
    response_model=JobAccepted,
    status_code=202,
    summary="Submit an analytics job (analyst+). Returns 202: accepted, not done.",
    responses={403: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}, 503: {"model": ErrorEnvelope}},
)
def submit_job(
    body: JobCreate,
    request: Request,
    principal: Principal = Depends(require_role(Role.ANALYST)),
    db: Session = Depends(get_db),
):
    """The server has accepted responsibility for the work but has not done it. The work
    is divided into (day x department) partitions and run MapReduce-style on the worker
    pool; poll `poll_url` to watch `partitions_completed` climb."""
    job = svc.create_job(db, body, principal.user_id, _cid(request))
    return _accepted(job)


@router.get("", response_model=JobPage, summary="List jobs (analyst+)")
def list_jobs(
    _: Principal = Depends(require_role(Role.ANALYST)),
    db: Session = Depends(get_db),
    status: JobStatus | None = None,
    limit: int = Query(25, ge=1, le=200),
    cursor: str | None = None,
):
    rows, nxt = svc.list_jobs(db, status.value if status else None, limit, decode_cursor(cursor))
    db.close()
    return JobPage(data=[svc.to_out(j) for j in rows], next_cursor=encode_cursor(nxt) if nxt else None)


@router.get(
    "/{job_id}",
    response_model=JobOut,
    summary="Job status, progress and duration (analyst+)",
    responses={404: {"model": ErrorEnvelope}},
)
def get_job(job_id: uuid.UUID, _: Principal = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    job = svc.get_job_or_404(db, job_id)
    db.close()  # the Redis progress lookup below must not hold a pooled connection
    return svc.to_out(job)


def _assert_can_modify(job, principal: Principal) -> None:
    if principal.role != Role.ADMIN and job.requested_by != principal.user_id:
        raise APIError(403, "forbidden", "Analysts may only manage their own jobs")


@router.post(
    "/{job_id}/cancel",
    response_model=JobOut,
    summary="Cancel a queued or running job (own jobs; admin: any)",
    responses={403: {"model": ErrorEnvelope}, 404: {"model": ErrorEnvelope}, 409: {"model": ErrorEnvelope}},
)
def cancel_job(job_id: uuid.UUID, principal: Principal = Depends(require_role(Role.ANALYST)), db: Session = Depends(get_db)):
    job = svc.get_job_or_404(db, job_id)
    _assert_can_modify(job, principal)
    return svc.to_out(svc.cancel_job(db, job))


@router.post(
    "/{job_id}/rerun",
    response_model=JobAccepted,
    status_code=202,
    summary="Re-run a job with the same parameters (own jobs; admin: any)",
    responses={403: {"model": ErrorEnvelope}, 404: {"model": ErrorEnvelope}},
)
def rerun_job(
    job_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_role(Role.ANALYST)),
    db: Session = Depends(get_db),
):
    original = svc.get_job_or_404(db, job_id)
    _assert_can_modify(original, principal)
    job = svc.create_job(db, svc.clone_params_for_rerun(original), principal.user_id, _cid(request))
    return _accepted(job)
