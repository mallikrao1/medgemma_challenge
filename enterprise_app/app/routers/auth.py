from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..audit import write_audit_log
from ..database import get_db
from ..dependencies import get_current_user
from ..models import Tenant, User
from ..schemas import LoginRequest, TokenResponse, UserResponse
from ..security import create_access_token, verify_password


router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    tenant = db.query(Tenant).filter(Tenant.slug == payload.tenant_slug, Tenant.is_active.is_(True)).first()
    if tenant is None:
        write_audit_log(
            db,
            request,
            action="auth.login.failed",
            resource_type="user",
            status="failed",
            details={"reason": "tenant_not_found", "tenant_slug": payload.tenant_slug},
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    user = (
        db.query(User)
        .filter(User.tenant_id == tenant.id, User.email == payload.email, User.is_active.is_(True))
        .first()
    )
    if user is None or not verify_password(payload.password, user.hashed_password):
        write_audit_log(
            db,
            request,
            action="auth.login.failed",
            resource_type="user",
            status="failed",
            details={"reason": "invalid_password_or_user", "email": payload.email},
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token, expires_in = create_access_token(
        subject=user.id, tenant_id=user.tenant_id, role=user.role, email=user.email
    )
    write_audit_log(
        db,
        request,
        action="auth.login.success",
        resource_type="user",
        status="success",
        actor=user,
    )
    return TokenResponse(
        access_token=token,
        expires_in_seconds=expires_in,
        user=UserResponse.model_validate(user),
    )


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)) -> UserResponse:
    return UserResponse.model_validate(user)

