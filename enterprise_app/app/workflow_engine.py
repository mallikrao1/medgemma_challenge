import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .models import Job, WorkflowRun, WorkflowTemplate


def now_utc():
    return datetime.now(timezone.utc)


def _render_output(template: WorkflowTemplate, run: WorkflowRun, input_payload: dict[str, Any]) -> dict[str, Any]:
    try:
        definition = json.loads(template.definition_json or "{}")
    except json.JSONDecodeError:
        definition = {}
    steps = definition.get("steps") if isinstance(definition, dict) else []
    if not isinstance(steps, list):
        steps = []

    step_results: list[dict[str, Any]] = []
    for idx, step in enumerate(steps, start=1):
        name = str(step.get("name") or f"step-{idx}") if isinstance(step, dict) else f"step-{idx}"
        action = str(step.get("action") or "noop") if isinstance(step, dict) else "noop"
        step_results.append({"step": idx, "name": name, "action": action, "status": "completed"})

    return {
        "workflow_name": template.name,
        "workflow_version": template.version,
        "run_id": run.id,
        "input": input_payload,
        "steps": step_results,
        "summary": f"Workflow '{template.name}' completed with {len(step_results)} steps.",
    }


def execute_workflow_run(db: Session, run: WorkflowRun) -> WorkflowRun:
    template = db.query(WorkflowTemplate).filter(WorkflowTemplate.id == run.template_id).first()
    if template is None:
        run.status = "failed"
        run.error_message = "Workflow template not found"
        run.finished_at = now_utc()
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    run.status = "running"
    run.started_at = now_utc()
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        input_payload = json.loads(run.input_json or "{}")
    except json.JSONDecodeError:
        input_payload = {}

    output = _render_output(template, run, input_payload)
    run.output_json = json.dumps(output, ensure_ascii=True)
    run.status = "completed"
    run.finished_at = now_utc()
    run.error_message = None
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def create_workflow_job(db: Session, tenant_id: str, run_id: str, payload: dict[str, Any] | None = None) -> Job:
    job = Job(
        tenant_id=tenant_id,
        workflow_run_id=run_id,
        job_type="workflow.execute",
        status="queued",
        payload_json=json.dumps(payload or {}, ensure_ascii=True),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def dispatch_one_job(db: Session) -> Job | None:
    now = now_utc()
    job = (
        db.query(Job)
        .filter(
            Job.status.in_(["queued", "retry"]),
            Job.available_at <= now,
            Job.attempts < Job.max_attempts,
        )
        .order_by(Job.created_at.asc())
        .first()
    )
    if job is None:
        return None

    job.status = "running"
    job.started_at = now
    job.attempts += 1
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        if job.job_type == "workflow.execute" and job.workflow_run_id:
            run = db.query(WorkflowRun).filter(WorkflowRun.id == job.workflow_run_id).first()
            if run is None:
                raise RuntimeError("Workflow run not found for job")
            execute_workflow_run(db, run)
            job.result_json = json.dumps({"workflow_run_id": run.id, "final_status": run.status}, ensure_ascii=True)
            job.status = "completed"
            job.error_message = None
        else:
            job.result_json = json.dumps({"note": "No-op job type"}, ensure_ascii=True)
            job.status = "completed"
    except Exception as exc:
        job.status = "failed" if job.attempts >= job.max_attempts else "retry"
        job.error_message = str(exc)
    finally:
        job.finished_at = now_utc()
        db.add(job)
        db.commit()
        db.refresh(job)

    return job

