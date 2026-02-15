from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_roles
from ..models import AuditLog, User
from ..schemas import AuditLogResponse


router = APIRouter(prefix="/v1/audit-logs", tags=["audit"])


@router.get("", response_model=list[AuditLogResponse])
def list_audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    actor: User = Depends(require_roles("admin", "auditor")),
    db: Session = Depends(get_db),
) -> list[AuditLogResponse]:
    rows = (
        db.query(AuditLog)
        .filter((AuditLog.tenant_id == actor.tenant_id) | (AuditLog.tenant_id.is_(None)))
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return [AuditLogResponse.model_validate(row) for row in rows]

