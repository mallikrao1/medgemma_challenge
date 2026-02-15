from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..audit import write_audit_log
from ..database import get_db
from ..dependencies import require_roles
from ..models import User
from ..policy_engine import evaluate_policies
from ..schemas import UserCreateRequest, UserResponse
from ..security import get_password_hash


router = APIRouter(prefix="/v1/users", tags=["users"])


@router.get("", response_model=list[UserResponse])
def list_users(actor: User = Depends(require_roles("admin", "auditor")), db: Session = Depends(get_db)) -> list[UserResponse]:
    users = db.query(User).filter(User.tenant_id == actor.tenant_id).order_by(User.created_at.desc()).all()
    return [UserResponse.model_validate(user) for user in users]


@router.post("", response_model=UserResponse, status_code=201)
def create_user(
    payload: UserCreateRequest,
    request: Request,
    actor: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> UserResponse:
    if payload.role not in {"admin", "clinician", "auditor"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")

    allowed, triggered = evaluate_policies(
        db,
        actor,
        action="user.create",
        context={"new_user": {"email": payload.email, "role": payload.role}, "actor": {"role": actor.role}},
    )
    if not allowed:
        write_audit_log(
            db,
            request,
            action="user.create.denied",
            resource_type="user",
            status="failed",
            actor=actor,
            details={"triggered_rules": triggered},
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Policy denied user creation")

    existing = (
        db.query(User)
        .filter(User.tenant_id == actor.tenant_id, User.email == payload.email)
        .first()
    )
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists in tenant")

    user = User(
        tenant_id=actor.tenant_id,
        email=payload.email,
        full_name=payload.full_name,
        role=payload.role,
        hashed_password=get_password_hash(payload.password),
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    write_audit_log(
        db,
        request,
        action="user.create",
        resource_type="user",
        resource_id=user.id,
        actor=actor,
        details={"new_user_email": user.email, "new_user_role": user.role},
    )
    return UserResponse.model_validate(user)
