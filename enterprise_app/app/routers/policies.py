import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..audit import write_audit_log
from ..database import get_db
from ..dependencies import require_roles
from ..models import PolicyRule, User
from ..schemas import PolicyCreateRequest, PolicyResponse


router = APIRouter(prefix="/v1/policies", tags=["policies"])


@router.get("", response_model=list[PolicyResponse])
def list_policies(actor: User = Depends(require_roles("admin", "auditor")), db: Session = Depends(get_db)) -> list[PolicyResponse]:
    rows = (
        db.query(PolicyRule)
        .filter(PolicyRule.tenant_id == actor.tenant_id)
        .order_by(PolicyRule.created_at.desc())
        .all()
    )
    return [PolicyResponse.model_validate(row) for row in rows]


@router.post("", response_model=PolicyResponse, status_code=201)
def create_policy(
    payload: PolicyCreateRequest,
    request: Request,
    actor: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    if payload.effect not in {"allow", "deny"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid effect")
    policy = PolicyRule(
        tenant_id=actor.tenant_id,
        name=payload.name,
        target_action=payload.target_action,
        effect=payload.effect,
        condition_json=json.dumps(payload.condition, ensure_ascii=True),
        is_active=True,
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)

    write_audit_log(
        db,
        request,
        action="policy.create",
        resource_type="policy",
        resource_id=policy.id,
        actor=actor,
        details={"target_action": policy.target_action, "effect": policy.effect},
    )
    return PolicyResponse.model_validate(policy)


@router.post("/{policy_id}/toggle", response_model=PolicyResponse)
def toggle_policy(
    policy_id: str,
    request: Request,
    actor: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    policy = (
        db.query(PolicyRule)
        .filter(PolicyRule.id == policy_id, PolicyRule.tenant_id == actor.tenant_id)
        .first()
    )
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found")
    policy.is_active = not policy.is_active
    db.add(policy)
    db.commit()
    db.refresh(policy)
    write_audit_log(
        db,
        request,
        action="policy.toggle",
        resource_type="policy",
        resource_id=policy.id,
        actor=actor,
        details={"is_active": policy.is_active},
    )
    return PolicyResponse.model_validate(policy)

