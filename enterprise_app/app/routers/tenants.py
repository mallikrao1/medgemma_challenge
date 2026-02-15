from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..audit import write_audit_log
from ..database import get_db
from ..dependencies import require_roles
from ..models import Tenant, User
from ..schemas import TenantCreateRequest, TenantResponse


router = APIRouter(prefix="/v1/tenants", tags=["tenants"])


@router.get("", response_model=list[TenantResponse])
def list_tenants(_: User = Depends(require_roles("admin")), db: Session = Depends(get_db)) -> list[TenantResponse]:
    tenants = db.query(Tenant).order_by(Tenant.created_at.desc()).all()
    return [TenantResponse.model_validate(tenant) for tenant in tenants]


@router.post("", response_model=TenantResponse, status_code=201)
def create_tenant(
    payload: TenantCreateRequest,
    request: Request,
    actor: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> TenantResponse:
    existing = db.query(Tenant).filter(Tenant.slug == payload.slug).first()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant slug already exists")

    tenant = Tenant(name=payload.name, slug=payload.slug, is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    write_audit_log(
        db,
        request,
        action="tenant.create",
        resource_type="tenant",
        resource_id=tenant.id,
        actor=actor,
        details={"slug": tenant.slug, "name": tenant.name},
    )
    return TenantResponse.model_validate(tenant)

