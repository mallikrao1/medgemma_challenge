from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..audit import write_audit_log
from ..database import get_db
from ..dependencies import require_roles
from ..models import Job, User
from ..schemas import JobResponse
from ..workflow_engine import dispatch_one_job


router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


@router.get("", response_model=list[JobResponse])
def list_jobs(actor: User = Depends(require_roles("admin", "auditor")), db: Session = Depends(get_db)) -> list[JobResponse]:
    rows = (
        db.query(Job)
        .filter(Job.tenant_id == actor.tenant_id)
        .order_by(Job.created_at.desc())
        .limit(200)
        .all()
    )
    return [JobResponse.model_validate(row) for row in rows]


@router.post("/dispatch-once", response_model=JobResponse | None)
def dispatch_once(
    request: Request,
    actor: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> JobResponse | None:
    job = dispatch_one_job(db)
    write_audit_log(
        db,
        request,
        action="job.dispatch_once",
        resource_type="job",
        resource_id=job.id if job else None,
        actor=actor,
        details={"dispatched": bool(job)},
    )
    if not job:
        return None
    return JobResponse.model_validate(job)

