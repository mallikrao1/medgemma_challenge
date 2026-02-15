from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    email: str
    password: str = Field(..., min_length=8)
    tenant_slug: str = Field(default="default", min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int
    user: "UserResponse"


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: datetime


class TenantCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    slug: str = Field(..., min_length=2, max_length=120)


class TenantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    is_active: bool
    created_at: datetime


class UserCreateRequest(BaseModel):
    email: str
    full_name: str = Field(..., min_length=2, max_length=200)
    role: str = Field(default="clinician")
    password: str = Field(..., min_length=8)


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    request_id: str
    actor_user_id: str | None
    actor_email: str | None
    tenant_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    status: str
    details_json: str
    created_at: datetime


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str


class ErrorResponse(BaseModel):
    detail: str
    context: dict[str, Any] | None = None


class PolicyCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    target_action: str = Field(..., min_length=2, max_length=120)
    effect: str = Field(default="deny")
    condition: dict[str, Any] = Field(default_factory=dict)


class PolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    name: str
    target_action: str
    effect: str
    condition_json: str
    is_active: bool
    created_at: datetime


class WorkflowTemplateCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    version: str = Field(default="1.0.0", min_length=1, max_length=40)
    definition: dict[str, Any] = Field(default_factory=dict)


class WorkflowTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    name: str
    version: str
    definition_json: str
    is_active: bool
    created_at: datetime


class WorkflowRunCreateRequest(BaseModel):
    input_data: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    template_id: str
    requested_by_user_id: str
    status: str
    input_json: str
    output_json: str
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    workflow_run_id: str | None
    job_type: str
    status: str
    attempts: int
    max_attempts: int
    available_at: datetime
    payload_json: str
    result_json: str
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
