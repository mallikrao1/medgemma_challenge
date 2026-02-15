import json
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from .models import AuditLog, User


def write_audit_log(
    db: Session,
    request: Request,
    action: str,
    resource_type: str,
    status: str = "success",
    resource_id: str | None = None,
    actor: User | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    request_id = getattr(request.state, "request_id", "unknown")
    payload = AuditLog(
        request_id=str(request_id),
        actor_user_id=actor.id if actor else None,
        actor_email=actor.email if actor else None,
        tenant_id=actor.tenant_id if actor else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        status=status,
        details_json=json.dumps(details or {}, ensure_ascii=True),
    )
    db.add(payload)
    db.commit()

