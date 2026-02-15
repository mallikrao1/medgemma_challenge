import json

from sqlalchemy.orm import Session

from .config import settings
from .models import Tenant, User, WorkflowTemplate
from .security import get_password_hash


def seed_default_data(db: Session) -> None:
    tenant = db.query(Tenant).filter(Tenant.slug == settings.bootstrap_tenant_slug).first()
    if tenant is None:
        tenant = Tenant(name=settings.bootstrap_tenant_name, slug=settings.bootstrap_tenant_slug, is_active=True)
        db.add(tenant)
        db.commit()
        db.refresh(tenant)

    admin = (
        db.query(User)
        .filter(User.tenant_id == tenant.id, User.email == settings.bootstrap_admin_email)
        .first()
    )
    if admin is None:
        admin = User(
            tenant_id=tenant.id,
            email=settings.bootstrap_admin_email,
            full_name=settings.bootstrap_admin_full_name,
            role="admin",
            hashed_password=get_password_hash(settings.bootstrap_admin_password),
            is_active=True,
        )
        db.add(admin)
        db.commit()

    # Seed one starter workflow template for immediate Sprint-2 testing.
    template = (
        db.query(WorkflowTemplate)
        .filter(WorkflowTemplate.tenant_id == tenant.id, WorkflowTemplate.name == "discharge_followup", WorkflowTemplate.version == "1.0.0")
        .first()
    )
    if template is None:
        template = WorkflowTemplate(
            tenant_id=tenant.id,
            name="discharge_followup",
            version="1.0.0",
            definition_json=json.dumps(
                {
                    "description": "Post-discharge follow-up workflow",
                    "steps": [
                        {"name": "validate_input", "action": "schema.validate"},
                        {"name": "policy_check", "action": "policy.evaluate"},
                        {"name": "generate_plan", "action": "ai.generate"},
                        {"name": "human_review_gate", "action": "review.queue"},
                    ],
                },
                ensure_ascii=True,
            ),
            is_active=True,
        )
        db.add(template)
        db.commit()
