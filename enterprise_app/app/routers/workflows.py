import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..audit import write_audit_log
from ..database import get_db
from ..dependencies import require_roles
from ..models import User, WorkflowRun, WorkflowTemplate
from ..policy_engine import evaluate_policies
from ..schemas import (
    WorkflowRunCreateRequest,
    WorkflowRunResponse,
    WorkflowTemplateCreateRequest,
    WorkflowTemplateResponse,
)
from ..workflow_engine import create_workflow_job


router = APIRouter(prefix="/v1/workflows", tags=["workflows"])


@router.get("/templates", response_model=list[WorkflowTemplateResponse])
def list_templates(
    actor: User = Depends(require_roles("admin", "clinician", "auditor")),
    db: Session = Depends(get_db),
) -> list[WorkflowTemplateResponse]:
    rows = (
        db.query(WorkflowTemplate)
        .filter(WorkflowTemplate.tenant_id == actor.tenant_id)
        .order_by(WorkflowTemplate.created_at.desc())
        .all()
    )
    return [WorkflowTemplateResponse.model_validate(row) for row in rows]


@router.post("/templates", response_model=WorkflowTemplateResponse, status_code=201)
def create_template(
    payload: WorkflowTemplateCreateRequest,
    request: Request,
    actor: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> WorkflowTemplateResponse:
    allowed, triggered = evaluate_policies(
        db,
        actor,
        action="workflow.template.create",
        context={"template": payload.model_dump(), "actor": {"role": actor.role}},
    )
    if not allowed:
        write_audit_log(
            db,
            request,
            action="workflow.template.create.denied",
            resource_type="workflow_template",
            status="failed",
            actor=actor,
            details={"triggered_rules": triggered},
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Policy denied template creation")

    existing = (
        db.query(WorkflowTemplate)
        .filter(
            WorkflowTemplate.tenant_id == actor.tenant_id,
            WorkflowTemplate.name == payload.name,
            WorkflowTemplate.version == payload.version,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Template version already exists")

    row = WorkflowTemplate(
        tenant_id=actor.tenant_id,
        name=payload.name,
        version=payload.version,
        definition_json=json.dumps(payload.definition, ensure_ascii=True),
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    write_audit_log(
        db,
        request,
        action="workflow.template.create",
        resource_type="workflow_template",
        resource_id=row.id,
        actor=actor,
        details={"name": row.name, "version": row.version},
    )
    return WorkflowTemplateResponse.model_validate(row)


@router.get("/runs", response_model=list[WorkflowRunResponse])
def list_runs(
    actor: User = Depends(require_roles("admin", "clinician", "auditor")),
    db: Session = Depends(get_db),
) -> list[WorkflowRunResponse]:
    rows = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.tenant_id == actor.tenant_id)
        .order_by(WorkflowRun.created_at.desc())
        .limit(200)
        .all()
    )
    return [WorkflowRunResponse.model_validate(row) for row in rows]


@router.post("/templates/{template_id}/runs", response_model=WorkflowRunResponse, status_code=201)
def create_run(
    template_id: str,
    payload: WorkflowRunCreateRequest,
    request: Request,
    actor: User = Depends(require_roles("admin", "clinician")),
    db: Session = Depends(get_db),
) -> WorkflowRunResponse:
    template = (
        db.query(WorkflowTemplate)
        .filter(WorkflowTemplate.id == template_id, WorkflowTemplate.tenant_id == actor.tenant_id, WorkflowTemplate.is_active.is_(True))
        .first()
    )
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow template not found")

    allowed, triggered = evaluate_policies(
        db,
        actor,
        action="workflow.run.create",
        context={
            "actor": {"role": actor.role, "email": actor.email},
            "workflow": {"template_id": template.id, "template_name": template.name},
            "input": payload.input_data,
        },
    )
    if not allowed:
        write_audit_log(
            db,
            request,
            action="workflow.run.create.denied",
            resource_type="workflow_run",
            status="failed",
            actor=actor,
            details={"triggered_rules": triggered},
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Policy denied workflow execution")

    run = WorkflowRun(
        tenant_id=actor.tenant_id,
        template_id=template.id,
        requested_by_user_id=actor.id,
        status="queued",
        input_json=json.dumps(payload.input_data, ensure_ascii=True),
        output_json="{}",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    job = create_workflow_job(db, tenant_id=actor.tenant_id, run_id=run.id, payload={"requested_by": actor.email})
    write_audit_log(
        db,
        request,
        action="workflow.run.create",
        resource_type="workflow_run",
        resource_id=run.id,
        actor=actor,
        details={"template_id": template.id, "job_id": job.id},
    )
    return WorkflowRunResponse.model_validate(run)

