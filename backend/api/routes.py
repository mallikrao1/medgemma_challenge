"""
API Routes - Dynamic AWS Infrastructure Agent
"""

from fastapi import APIRouter, HTTPException, Request, Header
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import uuid
import time
from config import settings

router = APIRouter()


class InfrastructureRequest(BaseModel):
    natural_language_request: str
    environment: str = "dev"
    cloud_provider: str = "aws"
    aws_access_key: Optional[str] = None
    aws_secret_key: Optional[str] = None
    aws_region: Optional[str] = "us-west-2"
    input_variables: Optional[Dict[str, Any]] = None


class PromptImproveRequest(BaseModel):
    natural_language_request: str
    environment: str = "dev"
    aws_region: Optional[str] = "us-east-1"


class EC2StatusRequest(BaseModel):
    instance_id: str
    aws_access_key: str
    aws_secret_key: str
    aws_region: Optional[str] = "us-east-1"


class EC2DeployRequest(BaseModel):
    instance_id: str
    aws_access_key: str
    aws_secret_key: str
    aws_region: Optional[str] = "us-east-1"
    app_targets: Optional[List[str]] = None
    app_port: Optional[int] = None
    public_access: Optional[bool] = False
    custom_commands: Optional[List[str]] = None
    wait_seconds: Optional[int] = 300
    request_id: Optional[str] = None
    environment: Optional[str] = "dev"
    request_text: Optional[str] = None


class ResourceStatusRequest(BaseModel):
    resource_type: str
    resource_name: str
    aws_access_key: str
    aws_secret_key: str
    aws_region: Optional[str] = "us-east-1"


class ResourceDiscoveryRequest(BaseModel):
    aws_access_key: str
    aws_secret_key: str
    aws_region: Optional[str] = "us-east-1"
    resource_types: Optional[List[str]] = None
    per_type_limit: Optional[int] = 10
    include_empty: Optional[bool] = False


class RDSSQLExecuteRequest(BaseModel):
    rds_instance_id: str
    sql: str
    aws_access_key: str
    aws_secret_key: str
    aws_region: Optional[str] = "us-east-1"
    db_name: Optional[str] = None
    db_username: Optional[str] = None
    db_password: Optional[str] = None
    secret_arn: Optional[str] = None
    bastion_instance_id: Optional[str] = None
    ensure_network_path: Optional[bool] = True
    wait_seconds: Optional[int] = 300
    request_id: Optional[str] = None
    environment: Optional[str] = "dev"
    request_text: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"
    permissions: Optional[List[Dict[str, Any]]] = None


class SetPasswordRequest(BaseModel):
    password: str


class SetPermissionsRequest(BaseModel):
    permissions: List[Dict[str, Any]]


class SetUserStatusRequest(BaseModel):
    is_active: bool


class RemediationPreviewRequest(BaseModel):
    request_id: str
    run_id: str


class RemediationExecuteRequest(BaseModel):
    request_id: str
    run_id: str
    approved: bool
    note: Optional[str] = None


SUPPORTED_SERVICES = [
    "S3 Buckets", "EC2 Instances", "RDS Databases", "Lambda Functions",
    "VPC Networks", "IAM Roles/Users/Policies", "DynamoDB Tables",
    "SNS Topics", "SQS Queues", "ECS Clusters", "EKS Clusters",
    "Route53 DNS", "CloudFront CDN", "CloudWatch Alarms",
    "Security Groups", "EBS Volumes", "EFS File Systems",
    "Secrets Manager", "SSM Parameters", "ECR Repositories",
    "KMS Keys", "ACM Certificates", "API Gateway",
    "Step Functions", "ElastiCache", "Kinesis Streams",
    "CodePipeline", "CodeBuild", "Redshift", "EMR", "SageMaker",
    "Glue", "Athena", "WAF", "Well-Architected Tool",
]

SUPPORTED_RESOURCE_TYPES = [
    "s3", "ec2", "rds", "lambda", "vpc", "iam", "dynamodb", "sns", "sqs", "ecs", "eks",
    "route53", "cloudfront", "cloudwatch", "security_group", "ebs", "efs", "secretsmanager", "ssm",
    "ecr", "kms", "acm", "apigateway", "stepfunctions", "elasticache", "kinesis", "codepipeline",
    "codebuild", "redshift", "emr", "sagemaker", "glue", "athena", "waf", "wellarchitected",
    "subnet", "nat_gateway", "internet_gateway", "eip", "log_group", "elb",
]


def _get_auth_service(app_request: Request):
    service = getattr(app_request.app.state, "auth_service", None)
    if not service:
        raise HTTPException(status_code=500, detail="Auth service not initialized")
    return service


def _extract_token(authorization: Optional[str]) -> str:
    value = str(authorization or "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def _require_user(app_request: Request, authorization: Optional[str]) -> Dict[str, Any]:
    auth_service = _get_auth_service(app_request)
    token = _extract_token(authorization)
    user = auth_service.authenticate_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def _require_admin(app_request: Request, authorization: Optional[str]) -> Dict[str, Any]:
    user = _require_user(app_request, authorization)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _require_permission(
    app_request: Request,
    user: Dict[str, Any],
    resource_type: str,
    capabilities: List[str],
):
    auth_service = _get_auth_service(app_request)
    if not auth_service.has_permissions(user, resource_type, capabilities):
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied for {resource_type}: requires {', '.join(capabilities)}",
        )


def _capabilities_for_action(action: str) -> List[str]:
    text = str(action or "").strip().lower()
    if text in {"list", "describe", "get", "read"}:
        return ["read"]
    if text in {"create", "update", "delete", "deploy", "execute"}:
        return ["write", "execute"]
    return ["execute"]


def _remediation_requires_iam(remediation_plan: Dict[str, Any]) -> bool:
    perms = remediation_plan.get("required_permissions", []) if isinstance(remediation_plan, dict) else []
    for item in perms:
        token = str(item or "").strip().lower()
        if token.startswith("iam:") or token.startswith("sts:"):
            return True
    return False


def _normalize_resource_status(resource_type: str, resource_name: str, describe_result: Dict[str, Any]) -> Dict[str, Any]:
    rt = str(resource_type or "").strip().lower()
    pending_tokens = (
        "creating",
        "pending",
        "initializing",
        "starting",
        "provisioning",
        "inprogress",
        "in_progress",
        "modifying",
        "updating",
        "configuring",
    )
    ready_tokens = (
        "available",
        "active",
        "running",
        "ready",
        "ok",
        "completed",
        "enabled",
        "issued",
        "inservice",
    )

    def classify(value: Any) -> Dict[str, Any]:
        state_text = str(value or "unknown")
        lowered = state_text.strip().lower().replace(" ", "")
        if any(token in lowered for token in pending_tokens):
            return {"ready": False, "state": state_text}
        if any(token in lowered for token in ready_tokens):
            return {"ready": True, "state": state_text}
        return {"ready": bool(describe_result.get("success")), "state": state_text}

    normalized = {
        "success": bool(describe_result.get("success")),
        "resource_type": rt,
        "resource_name": resource_name,
        "ready": False,
        "state": None,
        "message": "",
        "raw": describe_result,
    }

    if not describe_result.get("success"):
        normalized["message"] = describe_result.get("error") or "Failed to fetch resource status."
        return normalized

    if rt == "rds":
        state = str(describe_result.get("status") or "unknown")
        ready = state.lower() == "available"
        normalized.update({
            "state": state,
            "ready": ready,
            "message": "RDS is available." if ready else f"RDS status is {state}.",
        })
        return normalized

    if rt == "eks":
        state = str(describe_result.get("status") or "unknown")
        ready = state.upper() == "ACTIVE"
        normalized.update({
            "state": state,
            "ready": ready,
            "message": "EKS cluster is active." if ready else f"EKS cluster status is {state}.",
        })
        return normalized

    if rt == "lambda":
        state = str(describe_result.get("state") or "Unknown")
        ready = state.lower() in {"active", "unknown"}
        normalized.update({
            "state": state,
            "ready": ready,
            "message": "Lambda is active." if ready else f"Lambda state is {state}.",
        })
        return normalized

    if rt == "s3":
        normalized.update({
            "state": "available",
            "ready": True,
            "message": "S3 bucket is reachable.",
        })
        return normalized

    if rt in {"ecs", "emr", "elasticache", "redshift", "sagemaker", "codebuild", "codepipeline", "glue", "athena", "cloudfront", "elb", "apigateway"}:
        status_value = describe_result.get("status") or describe_result.get("state") or describe_result.get("lifecycle_state")
        cls = classify(status_value)
        normalized.update({
            "state": cls["state"],
            "ready": cls["ready"],
            "message": f"{rt.upper()} status is {cls['state']}.",
        })
        return normalized

    cls = classify(describe_result.get("status") or describe_result.get("state") or "unknown")
    normalized.update({"state": cls["state"], "ready": cls["ready"], "message": "Status fetched."})
    return normalized


@router.post("/auth/login")
async def login(request: LoginRequest, app_request: Request):
    auth_service = _get_auth_service(app_request)
    result = auth_service.login(request.username, request.password)
    if not result:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return result


@router.post("/auth/logout")
async def logout(app_request: Request, authorization: Optional[str] = Header(default=None)):
    _require_user(app_request, authorization)
    auth_service = _get_auth_service(app_request)
    token = _extract_token(authorization)
    auth_service.revoke_session(token)
    return {"success": True}


@router.get("/auth/me")
async def auth_me(app_request: Request, authorization: Optional[str] = Header(default=None)):
    user = _require_user(app_request, authorization)
    auth_service = _get_auth_service(app_request)
    return {
        "user": user,
        "permissions": auth_service.list_permissions(int(user["id"])),
    }


@router.get("/deployments")
async def list_deployments(
    app_request: Request,
    limit: int = 100,
    all_users: bool = False,
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(app_request, authorization)
    auth_service = _get_auth_service(app_request)
    items = auth_service.list_deployments(user=user, limit=limit, all_users=all_users)
    return {
        "deployments": items,
        "count": len(items),
        "scope": "all" if (all_users and user.get("role") == "admin") else "mine",
    }


@router.get("/deployments/{request_id}")
async def list_deployment_history(
    request_id: str,
    app_request: Request,
    limit: int = 200,
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(app_request, authorization)
    auth_service = _get_auth_service(app_request)
    items = auth_service.list_deployments_by_request(user=user, request_id=request_id, limit=limit)
    return {
        "request_id": request_id,
        "deployments": items,
        "count": len(items),
    }


@router.get("/admin/users")
async def admin_list_users(app_request: Request, authorization: Optional[str] = Header(default=None)):
    _require_admin(app_request, authorization)
    auth_service = _get_auth_service(app_request)
    return {
        "users": auth_service.list_users(),
    }


@router.post("/admin/users")
async def admin_create_user(
    request: CreateUserRequest,
    app_request: Request,
    authorization: Optional[str] = Header(default=None),
):
    admin = _require_admin(app_request, authorization)
    auth_service = _get_auth_service(app_request)
    try:
        user = auth_service.create_user(
            admin_user_id=int(admin["id"]),
            username=request.username,
            password=request.password,
            role=request.role,
            permissions=request.permissions,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "user": user}


@router.put("/admin/users/{user_id}/password")
async def admin_set_password(
    user_id: int,
    request: SetPasswordRequest,
    app_request: Request,
    authorization: Optional[str] = Header(default=None),
):
    _require_admin(app_request, authorization)
    auth_service = _get_auth_service(app_request)
    try:
        auth_service.set_password(user_id=user_id, new_password=request.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True}


@router.put("/admin/users/{user_id}/permissions")
async def admin_set_permissions(
    user_id: int,
    request: SetPermissionsRequest,
    app_request: Request,
    authorization: Optional[str] = Header(default=None),
):
    _require_admin(app_request, authorization)
    auth_service = _get_auth_service(app_request)
    if not auth_service.get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    permissions = auth_service.set_permissions(user_id=user_id, permissions=request.permissions)
    return {"success": True, "permissions": permissions}


@router.put("/admin/users/{user_id}/status")
async def admin_set_user_status(
    user_id: int,
    request: SetUserStatusRequest,
    app_request: Request,
    authorization: Optional[str] = Header(default=None),
):
    admin = _require_admin(app_request, authorization)
    if int(admin["id"]) == int(user_id) and not bool(request.is_active):
        raise HTTPException(status_code=400, detail="Admin cannot deactivate own account")
    auth_service = _get_auth_service(app_request)
    try:
        auth_service.set_user_active(user_id=user_id, is_active=bool(request.is_active))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True}


@router.post("/prompt/improve")
async def improve_prompt(
    request: PromptImproveRequest,
    app_request: Request,
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(app_request, authorization)
    orchestrator = app_request.app.state.orchestrator
    try:
        intent_preview = await orchestrator.nlu_service.parse_request(
            request.natural_language_request,
            user_region=request.aws_region,
        )
        _require_permission(
            app_request,
            user,
            intent_preview.resource_type or "all",
            _capabilities_for_action(intent_preview.action or "execute"),
        )
        result = await orchestrator.improve_prompt(
            natural_language_request=request.natural_language_request,
            environment=request.environment,
            aws_region=request.aws_region,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ec2/status")
async def ec2_status(
    request: EC2StatusRequest,
    app_request: Request,
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(app_request, authorization)
    _require_permission(app_request, user, "ec2", ["read"])
    orchestrator = app_request.app.state.orchestrator
    try:
        orchestrator.aws_executor.initialize(
            request.aws_access_key,
            request.aws_secret_key,
            request.aws_region or "us-east-1",
        )
        result = await orchestrator.aws_executor.check_ec2_instance_readiness(request.instance_id)
        if result.get("success") and not result.get("ready"):
            result["next_retry_seconds"] = 30
        if result.get("success") and result.get("ready"):
            ssm_state = await orchestrator.aws_executor.is_instance_ssm_managed(request.instance_id)
            if ssm_state.get("success") and not ssm_state.get("managed"):
                result["remediation_hint"] = "ssm_prereq_missing"
                result["remediation_message"] = (
                    "Instance is ready but not yet managed by Systems Manager. "
                    "Auto-remediation can attach managed role/profile and continue deployment."
                )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ec2/deploy")
async def ec2_deploy(
    request: EC2DeployRequest,
    app_request: Request,
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(app_request, authorization)
    _require_permission(app_request, user, "ec2", ["write", "execute"])
    orchestrator = app_request.app.state.orchestrator
    auth_service = _get_auth_service(app_request)
    try:
        orchestrator.aws_executor.initialize(
            request.aws_access_key,
            request.aws_secret_key,
            request.aws_region or "us-east-1",
        )
        result = await orchestrator.aws_executor.deploy_to_ec2_via_ssm(
            instance_id=request.instance_id,
            app_targets=request.app_targets or [],
            app_port=request.app_port,
            public_access=bool(request.public_access),
            custom_commands=request.custom_commands or [],
            wait_seconds=request.wait_seconds or 300,
        )
        if not result.get("success"):
            remediation = orchestrator.remediation_engine.build_plan(
                intent={
                    "action": "deploy",
                    "resource_type": "ec2",
                    "resource_name": request.instance_id,
                    "region": request.aws_region or "us-east-1",
                    "parameters": {
                        "instance_id": request.instance_id,
                        "app_targets": request.app_targets or [],
                        "app_port": request.app_port,
                        "public_access": bool(request.public_access),
                        "custom_commands": request.custom_commands or [],
                    },
                },
                execution_result=result,
                context={"environment": request.environment or "dev", "region": request.aws_region or "us-east-1"},
            )
            if remediation and remediation.get("run_id"):
                req_id = str(request.request_id or f"adhoc-{uuid.uuid4().hex[:10]}")
                run = auth_service.create_remediation_run(
                    run_id=str(remediation.get("run_id")),
                    request_id=req_id,
                    user=user,
                    plan=remediation,
                    request_snapshot={
                        "request_id": req_id,
                        "requester_id": user.get("username", "user"),
                        "environment": request.environment or "dev",
                        "cloud_provider": "aws",
                        "natural_language_request": request.request_text or f"Deploy to EC2 {request.instance_id}",
                        "aws_access_key": request.aws_access_key,
                        "aws_secret_key": request.aws_secret_key,
                        "aws_region": request.aws_region or "us-east-1",
                        "input_variables": {},
                    },
                    resume_context={
                        "intent": {
                            "action": "create",
                            "resource_type": "ec2",
                            "resource_name": request.instance_id,
                            "region": request.aws_region or "us-east-1",
                            "parameters": {
                                "instance_id": request.instance_id,
                                "app_targets": request.app_targets or [],
                                "app_port": request.app_port,
                                "public_access": bool(request.public_access),
                                "custom_commands": request.custom_commands or [],
                                "wait_seconds": request.wait_seconds or 300,
                            },
                        },
                        "execution_result": result,
                        "next_phase": "deploy_app",
                        "phases": [
                            {"id": "design_plan", "title": "Design + plan", "status": "completed"},
                            {"id": "networking_security", "title": "Provision networking/security", "status": "completed"},
                            {"id": "compute_data", "title": "Provision compute/data", "status": "completed"},
                            {"id": "deploy_app", "title": "Deploy app", "status": "failed"},
                            {"id": "validate_health", "title": "Validate health checks", "status": "pending"},
                        ],
                    },
                    required_permissions=remediation.get("required_permissions", []),
                    approval_scope=remediation.get("approval_scope", "request_run"),
                )
                result["requires_input"] = True
                result["question_prompt"] = remediation.get("reason")
                result["questions"] = [
                    {
                        "variable": "remediation_approval",
                        "question": "Approve automatic remediation and continue? (approve/deny)",
                        "type": "string",
                        "options": ["approve", "deny"],
                    }
                ]
                result["continuation"] = {
                    "kind": "auto_remediation",
                    "run_id": run.get("run_id"),
                    "request_id": run.get("request_id"),
                    "approval_scope": run.get("approval_scope", "request_run"),
                }
                result["remediation"] = {
                    **remediation,
                    "run_id": run.get("run_id"),
                    "approval_scope": run.get("approval_scope", "request_run"),
                }
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/resource/status")
async def resource_status(
    request: ResourceStatusRequest,
    app_request: Request,
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(app_request, authorization)
    resource_type = str(request.resource_type or "").strip().lower()
    if not resource_type:
        raise HTTPException(status_code=400, detail="resource_type is required")
    if not request.resource_name:
        raise HTTPException(status_code=400, detail="resource_name is required")
    _require_permission(app_request, user, resource_type, ["read"])

    orchestrator = app_request.app.state.orchestrator
    try:
        orchestrator.aws_executor.initialize(
            request.aws_access_key,
            request.aws_secret_key,
            request.aws_region or "us-east-1",
        )
        describe_result = await orchestrator.aws_executor.execute(
            action="describe",
            resource_type=resource_type,
            resource_name=request.resource_name,
            parameters={},
            tags={},
        )
        normalized = _normalize_resource_status(resource_type, request.resource_name, describe_result)
        if normalized.get("success") and not normalized.get("ready"):
            normalized["next_retry_seconds"] = 30
            normalized.setdefault("remediation_hint", "wait_until_ready")
        if normalized.get("success") and resource_type == "rds" and str(normalized.get("state", "")).lower() != "available":
            normalized["remediation_hint"] = "wait_until_available"
        return normalized
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/resources/discover")
async def discover_resources(
    request: ResourceDiscoveryRequest,
    app_request: Request,
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(app_request, authorization)
    orchestrator = app_request.app.state.orchestrator
    aws = orchestrator.aws_executor
    if not aws.initialize(request.aws_access_key, request.aws_secret_key, request.aws_region or "us-east-1"):
        raise HTTPException(status_code=400, detail="Unable to initialize AWS session with provided credentials.")

    # Read-level access is required to discover resources.
    if request.resource_types:
        for item in request.resource_types:
            _require_permission(app_request, user, str(item or "all"), ["read"])
    else:
        _require_permission(app_request, user, "all", ["read"])

    try:
        result = await aws.discover_account_resources(
            resource_types=request.resource_types,
            per_type_limit=int(request.per_type_limit or 10),
            include_empty=bool(request.include_empty),
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/remediation/preview")
async def remediation_preview(
    request: RemediationPreviewRequest,
    app_request: Request,
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(app_request, authorization)
    auth_service = _get_auth_service(app_request)
    run = auth_service.get_remediation_run(run_id=request.run_id, request_id=request.request_id, user=user)
    if not run:
        raise HTTPException(status_code=404, detail="Remediation run not found")
    if run.get("is_expired"):
        auth_service.update_remediation_run(run_id=request.run_id, status="expired", result={"error": "Approval expired"})
        raise HTTPException(status_code=400, detail="Remediation approval window expired for this run.")
    return {
        "success": True,
        "run": run,
    }


@router.post("/remediation/execute")
async def remediation_execute(
    request: RemediationExecuteRequest,
    app_request: Request,
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(app_request, authorization)
    auth_service = _get_auth_service(app_request)
    orchestrator = app_request.app.state.orchestrator
    run = auth_service.get_remediation_run(run_id=request.run_id, request_id=request.request_id, user=user)
    if not run:
        raise HTTPException(status_code=404, detail="Remediation run not found")
    if run.get("is_expired"):
        auth_service.update_remediation_run(run_id=request.run_id, status="expired", result={"error": "Approval expired"})
        raise HTTPException(status_code=400, detail="Remediation approval expired. Re-run the request.")

    plan = run.get("plan", {}) if isinstance(run.get("plan"), dict) else {}
    target_resource = str(plan.get("resource_type") or "all")
    target_action = str(plan.get("action") or "execute")
    if request.approved:
        _require_permission(app_request, user, target_resource, _capabilities_for_action(target_action))
    if request.approved and _remediation_requires_iam(plan):
        _require_permission(app_request, user, "iam", ["write", "execute"])

    auth_service.log_approval_event(
        run_id=request.run_id,
        request_id=request.request_id,
        user=user,
        approved=bool(request.approved),
        scope=run.get("approval_scope", "request_run"),
        note=request.note,
    )

    if not request.approved:
        auth_service.update_remediation_run(
            run_id=request.run_id,
            status="denied",
            result={"success": False, "status": "denied", "note": request.note or ""},
            increment_attempt=False,
        )
        return {
            "success": False,
            "status": "denied",
            "message": "Remediation denied. Agent paused for manual action.",
            "run_id": request.run_id,
        }

    if int(run.get("attempts") or 0) >= int(settings.AUTO_REMEDIATION_MAX_ATTEMPTS or 2):
        raise HTTPException(status_code=400, detail="Maximum remediation attempts reached for this run.")

    auth_service.update_remediation_run(run_id=request.run_id, status="in_progress", result={"approved": True})
    outcome = await orchestrator.execute_remediation_with_resume(remediation_run=run, approved=True)
    final_status = "completed" if outcome.get("success") else "failed"
    auth_service.update_remediation_run(
        run_id=request.run_id,
        status=final_status,
        result=outcome,
        increment_attempt=True,
    )

    # Audit resumed outcome in deployment timeline.
    exec_result = outcome.get("execution_result", {}) if isinstance(outcome.get("execution_result"), dict) else {}
    resumed_intent = outcome.get("intent", {}) if isinstance(outcome.get("intent"), dict) else {}
    auth_service.log_deployment(
        request_id=request.request_id,
        user=user,
        action=(resumed_intent or {}).get("action"),
        resource_type=(resumed_intent or {}).get("resource_type"),
        resource_name=(resumed_intent or {}).get("resource_name"),
        region=(resumed_intent or {}).get("region") or run.get("request_snapshot", {}).get("aws_region"),
        environment=run.get("request_snapshot", {}).get("environment"),
        status="completed" if exec_result.get("success") else "failed",
        request_text=run.get("request_snapshot", {}).get("natural_language_request"),
        execution_summary={
            "success": bool(exec_result.get("success")),
            "error": exec_result.get("error"),
            "remediation_run_id": request.run_id,
            "remediation_rule_id": (plan or {}).get("rule_id"),
        },
    )

    return {
        "success": bool(outcome.get("success")),
        "run_id": request.run_id,
        "status": final_status,
        "result": outcome,
    }


@router.post("/rds/sql")
async def rds_sql_execute(
    request: RDSSQLExecuteRequest,
    app_request: Request,
    authorization: Optional[str] = Header(default=None),
):
    user = _require_user(app_request, authorization)
    _require_permission(app_request, user, "rds", ["write", "execute"])
    orchestrator = app_request.app.state.orchestrator
    auth_service = _get_auth_service(app_request)
    try:
        orchestrator.aws_executor.initialize(
            request.aws_access_key,
            request.aws_secret_key,
            request.aws_region or "us-east-1",
        )
        result = await orchestrator.aws_executor.execute_rds_sql_via_ssm(
            rds_instance_id=request.rds_instance_id,
            sql=request.sql,
            db_name=request.db_name,
            db_username=request.db_username,
            db_password=request.db_password,
            secret_arn=request.secret_arn,
            bastion_instance_id=request.bastion_instance_id,
            ensure_network_path=bool(request.ensure_network_path),
            wait_seconds=request.wait_seconds or 300,
        )
        if not result.get("success"):
            remediation = orchestrator.remediation_engine.build_plan(
                intent={
                    "action": "update",
                    "resource_type": "rds",
                    "resource_name": request.rds_instance_id,
                    "region": request.aws_region or "us-east-1",
                    "parameters": {
                        "db_instance_id": request.rds_instance_id,
                        "bastion_instance_id": request.bastion_instance_id,
                    },
                },
                execution_result=result,
                context={"environment": request.environment or "dev", "region": request.aws_region or "us-east-1"},
            )
            if remediation and remediation.get("run_id"):
                req_id = str(request.request_id or f"adhoc-{uuid.uuid4().hex[:10]}")
                run = auth_service.create_remediation_run(
                    run_id=str(remediation.get("run_id")),
                    request_id=req_id,
                    user=user,
                    plan=remediation,
                    request_snapshot={
                        "request_id": req_id,
                        "requester_id": user.get("username", "user"),
                        "environment": request.environment or "dev",
                        "cloud_provider": "aws",
                        "natural_language_request": request.request_text or f"Run SQL on RDS {request.rds_instance_id}",
                        "aws_access_key": request.aws_access_key,
                        "aws_secret_key": request.aws_secret_key,
                        "aws_region": request.aws_region or "us-east-1",
                        "input_variables": {},
                    },
                    resume_context={
                        "intent": {
                            "action": "update",
                            "resource_type": "rds",
                            "resource_name": request.rds_instance_id,
                            "region": request.aws_region or "us-east-1",
                            "parameters": {
                                "run_sql_via_ssm": True,
                                "db_instance_id": request.rds_instance_id,
                                "sql": request.sql,
                                "db_name": request.db_name,
                                "db_username": request.db_username,
                                "db_password": request.db_password,
                                "secret_arn": request.secret_arn,
                                "bastion_instance_id": request.bastion_instance_id,
                                "ensure_network_path": bool(request.ensure_network_path),
                            },
                        },
                        "execution_result": result,
                        "next_phase": "deploy_app",
                        "phases": [
                            {"id": "design_plan", "title": "Design + plan", "status": "completed"},
                            {"id": "networking_security", "title": "Provision networking/security", "status": "completed"},
                            {"id": "compute_data", "title": "Provision compute/data", "status": "completed"},
                            {"id": "deploy_app", "title": "Deploy app", "status": "failed"},
                            {"id": "validate_health", "title": "Validate health checks", "status": "pending"},
                        ],
                    },
                    required_permissions=remediation.get("required_permissions", []),
                    approval_scope=remediation.get("approval_scope", "request_run"),
                )
                result["requires_input"] = True
                result["question_prompt"] = remediation.get("reason")
                result["questions"] = [
                    {
                        "variable": "remediation_approval",
                        "question": "Approve automatic remediation and continue? (approve/deny)",
                        "type": "string",
                        "options": ["approve", "deny"],
                    }
                ]
                result["continuation"] = {
                    "kind": "auto_remediation",
                    "run_id": run.get("run_id"),
                    "request_id": run.get("request_id"),
                    "approval_scope": run.get("approval_scope", "request_run"),
                }
                result["remediation"] = {
                    **remediation,
                    "run_id": run.get("run_id"),
                    "approval_scope": run.get("approval_scope", "request_run"),
                }
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/requests")
async def create_request(
    request: InfrastructureRequest,
    app_request: Request,
    authorization: Optional[str] = Header(default=None),
):
    started_at = time.perf_counter()
    request_id = str(uuid.uuid4())
    user = _require_user(app_request, authorization)
    auth_service = _get_auth_service(app_request)
    orchestrator = app_request.app.state.orchestrator
    mlflow_tracker = getattr(app_request.app.state, "mlflow_tracker", None)

    # Preflight permission gate before execution.
    try:
        intent_preview = await orchestrator.nlu_service.parse_request(
            request.natural_language_request,
            user_region=request.aws_region,
        )
        _require_permission(
            app_request,
            user,
            intent_preview.resource_type or "all",
            _capabilities_for_action(intent_preview.action or "execute"),
        )
    except HTTPException:
        raise
    except Exception:
        # If parser fails, enforce broad execute permission.
        _require_permission(app_request, user, "all", ["execute"])

    try:
        context = await orchestrator.process_request({
            "request_id": request_id,
            "requester_id": user.get("username", "user"),
            "environment": request.environment,
            "cloud_provider": request.cloud_provider,
            "natural_language_request": request.natural_language_request,
            "aws_access_key": request.aws_access_key,
            "aws_secret_key": request.aws_secret_key,
            "aws_region": request.aws_region,
            "input_variables": request.input_variables or {},
        })

        exec_result = context.execution_result if isinstance(context.execution_result, dict) else {}
        remediation = exec_result.get("remediation", {}) if isinstance(exec_result.get("remediation"), dict) else {}
        if remediation.get("required") and remediation.get("run_id"):
            run = auth_service.create_remediation_run(
                run_id=str(remediation.get("run_id")),
                request_id=request_id,
                user=user,
                plan=remediation,
                request_snapshot={
                    "request_id": request_id,
                    "requester_id": user.get("username", "user"),
                    "environment": request.environment,
                    "cloud_provider": request.cloud_provider,
                    "natural_language_request": request.natural_language_request,
                    "aws_access_key": request.aws_access_key,
                    "aws_secret_key": request.aws_secret_key,
                    "aws_region": request.aws_region,
                    "input_variables": request.input_variables or {},
                },
                resume_context=exec_result.get("resume_context") if isinstance(exec_result.get("resume_context"), dict) else None,
                required_permissions=remediation.get("required_permissions", []),
                approval_scope=remediation.get("approval_scope", "request_run"),
                expires_in_seconds=3600,
            )
            continuation = exec_result.get("continuation", {}) if isinstance(exec_result.get("continuation"), dict) else {}
            continuation.update(
                {
                    "kind": "auto_remediation",
                    "run_id": run.get("run_id"),
                    "request_id": request_id,
                    "approval_scope": run.get("approval_scope", "request_run"),
                }
            )
            exec_result["continuation"] = continuation
            exec_result["remediation"] = {
                **remediation,
                "run_id": run.get("run_id"),
                "approval_scope": run.get("approval_scope", "request_run"),
            }
            context.execution_result = exec_result

        is_success = (
            context.execution_result
            and context.execution_result.get("success")
        )
        needs_input = (
            context.execution_result
            and context.execution_result.get("requires_input")
        )

        status = "needs_input" if needs_input else ("completed" if is_success else "failed")

        auth_service.log_deployment(
            request_id=request_id,
            user=user,
            action=(context.intent or {}).get("action"),
            resource_type=(context.intent or {}).get("resource_type"),
            resource_name=(context.intent or {}).get("resource_name"),
            region=(context.intent or {}).get("region") or request.aws_region,
            environment=request.environment,
            status=status,
            request_text=request.natural_language_request,
            execution_summary={
                "success": bool(is_success),
                "error": context.error or ((context.execution_result or {}).get("error") if isinstance(context.execution_result, dict) else None),
                "final_outcome": (context.execution_result or {}).get("final_outcome") if isinstance(context.execution_result, dict) else None,
                "resource": (context.intent or {}).get("resource_type"),
                "action": (context.intent or {}).get("action"),
                "resume_context": (context.execution_result or {}).get("resume_context") if isinstance(context.execution_result, dict) else None,
                "continuation": (context.execution_result or {}).get("continuation") if isinstance(context.execution_result, dict) else None,
                "phases": (context.execution_result or {}).get("phases") if isinstance(context.execution_result, dict) else None,
                "execution_path": (context.execution_result or {}).get("execution_path") if isinstance(context.execution_result, dict) else None,
                "request_text": request.natural_language_request,
            },
        )
        if mlflow_tracker and hasattr(mlflow_tracker, "log_request_run"):
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            mlflow_tracker.log_request_run(
                request_id=request_id,
                user=user,
                request_payload={
                    "natural_language_request": request.natural_language_request,
                    "environment": request.environment,
                    "cloud_provider": request.cloud_provider,
                    "aws_region": request.aws_region,
                    "input_variables": request.input_variables or {},
                },
                intent=context.intent if isinstance(context.intent, dict) else {},
                execution_result=context.execution_result if isinstance(context.execution_result, dict) else {},
                status=status,
                duration_ms=duration_ms,
            )

        return {
            "request_id": request_id,
            "status": status,
            "workflow_state": context.current_state.value,
            "intent": context.intent,
            "execution_result": context.execution_result,
            "policy": context.context_data.get("policy"),
            "error": context.error,
            "user": user,
        }
    except HTTPException:
        raise
    except Exception as e:
        auth_service.log_deployment(
            request_id=request_id,
            user=user,
            action=None,
            resource_type=None,
            resource_name=None,
            region=request.aws_region,
            environment=request.environment,
            status="failed",
            request_text=request.natural_language_request,
            execution_summary={"success": False, "error": str(e)},
        )
        if mlflow_tracker and hasattr(mlflow_tracker, "log_request_run"):
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            mlflow_tracker.log_request_run(
                request_id=request_id,
                user=user,
                request_payload={
                    "natural_language_request": request.natural_language_request,
                    "environment": request.environment,
                    "cloud_provider": request.cloud_provider,
                    "aws_region": request.aws_region,
                    "input_variables": request.input_variables or {},
                },
                intent={},
                execution_result={"success": False, "error": str(e)},
                status="failed",
                duration_ms=duration_ms,
            )
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/mlflow/info")
async def mlflow_info(app_request: Request, authorization: Optional[str] = Header(default=None)):
    _require_user(app_request, authorization)
    tracker = getattr(app_request.app.state, "mlflow_tracker", None)
    available = bool(getattr(tracker, "available", False))
    return {
        "enabled": bool(settings.MLFLOW_ENABLED),
        "available": available,
        "tracking_uri": settings.MLFLOW_TRACKING_URI,
        "experiment_name": settings.MLFLOW_EXPERIMENT_NAME,
        "ui_url": settings.MLFLOW_UI_URL,
    }


@router.get("/providers")
async def list_providers(app_request: Request, authorization: Optional[str] = Header(default=None)):
    _require_user(app_request, authorization)
    return {
        "providers": [
            {"id": "aws", "name": "Amazon Web Services"},
            {"id": "azure", "name": "Microsoft Azure"},
            {"id": "gcp", "name": "Google Cloud Platform"},
        ]
    }


@router.get("/environments")
async def list_environments(app_request: Request, authorization: Optional[str] = Header(default=None)):
    _require_user(app_request, authorization)
    return {
        "environments": [
            {"id": "dev", "name": "Development", "approval_count": 0},
            {"id": "qa", "name": "QA", "approval_count": 1},
            {"id": "prod", "name": "Production", "approval_count": 2},
        ]
    }


@router.get("/services")
async def list_supported_services(app_request: Request, authorization: Optional[str] = Header(default=None)):
    _require_user(app_request, authorization)
    return {
        "services": SUPPORTED_SERVICES,
        "resource_types": SUPPORTED_RESOURCE_TYPES,
        "actions": ["create", "update", "delete", "list", "describe"],
        "count": len(SUPPORTED_SERVICES),
    }


@router.get("/health/detailed")
async def detailed_health():
    return {
        "status": "healthy",
        "components": {
            "api": "healthy",
            "nlu": "healthy",
            "aws_executor": "healthy",
        },
        "supported_services": len(SUPPORTED_SERVICES),
    }
