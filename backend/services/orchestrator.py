"""
Orchestration Service - Fully dynamic AWS infrastructure agent.
Uses NLU to understand exact user intent and executes accordingly.
Supports create, update, delete, list, describe for ALL AWS services.
"""

import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime
from enum import Enum
import json
import copy
import uuid
import re
from pathlib import Path
from dataclasses import dataclass, field

from config import settings
from services.aws_executor import AWSExecutor
from services.nlu_service import NLUService, ParsedIntent
from services.boto_codegen import BotoCodeGenService
from services.prerequisite_schema import PrerequisiteSchemaService
from services.outcome_adapters import OutcomeValidationRegistry
from services.remediation_engine import RemediationEngine


class WorkflowState(Enum):
    INTAKE = "intake"
    NLU_PARSING = "nlu_parsing"
    CONTEXT_BUILD = "context_build"
    RAG_RETRIEVAL = "rag_retrieval"
    POLICY_CHECK = "policy_check"
    AWS_EXECUTION = "aws_execution"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class WorkflowContext:
    request_id: str
    requester_id: str
    environment: str
    cloud_provider: str
    natural_language_request: str
    current_state: WorkflowState
    history: List[Dict[str, Any]] = field(default_factory=list)
    context_data: Dict[str, Any] = field(default_factory=dict)
    intent: Optional[Dict[str, Any]] = None
    generated_code: Optional[str] = None
    execution_result: Optional[Dict] = None
    error: Optional[str] = None

    def __post_init__(self):
        self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()


class OrchestrationService:
    def __init__(self, rag_service):
        self.rag_service = rag_service
        self.nlu_service = NLUService()
        self.aws_executor = AWSExecutor()
        self.boto_codegen = BotoCodeGenService()
        self.prerequisite_schema = PrerequisiteSchemaService()
        self.outcome_registry = OutcomeValidationRegistry(
            self.aws_executor,
            rules_path=settings.OUTCOME_ADAPTER_RULES_PATH,
        )
        remediation_rules_path = settings.REMEDIATION_RULES_PATH
        if not remediation_rules_path:
            remediation_rules_path = str(Path(__file__).resolve().parent.parent / "config" / "remediation_rules.json")
        self.remediation_engine = RemediationEngine(
            rules_path=remediation_rules_path,
            enabled=settings.AUTO_REMEDIATION_ENABLED,
            preview_only=settings.AUTO_REMEDIATION_PREVIEW_ONLY,
            max_attempts=settings.AUTO_REMEDIATION_MAX_ATTEMPTS,
        )

    async def process_request(self, request_data: Dict[str, Any]) -> WorkflowContext:
        input_variables = request_data.get("input_variables") or {}
        context = WorkflowContext(
            request_id=request_data["request_id"],
            requester_id=request_data["requester_id"],
            environment=request_data["environment"],
            cloud_provider=request_data["cloud_provider"],
            natural_language_request=request_data["natural_language_request"],
            current_state=WorkflowState.INTAKE,
        )
        self._initialize_phases(context)
        context.context_data["request_snapshot"] = {
            "request_id": request_data.get("request_id"),
            "requester_id": request_data.get("requester_id"),
            "environment": request_data.get("environment"),
            "cloud_provider": request_data.get("cloud_provider"),
            "natural_language_request": request_data.get("natural_language_request"),
            "aws_access_key": request_data.get("aws_access_key"),
            "aws_secret_key": request_data.get("aws_secret_key"),
            "aws_region": request_data.get("aws_region"),
            "input_variables": copy.deepcopy(input_variables),
        }
        resume_context = input_variables.get("resume_context") if isinstance(input_variables.get("resume_context"), dict) else None
        if input_variables.get("resume_skipped_only") and resume_context:
            context = await self._resume_skipped_stage(
                context=context,
                request_data=request_data,
                input_variables=input_variables,
                resume_context=resume_context,
            )
            self._attach_resume_context(context)
            self._attach_phases_to_result(context)
            context.current_state = WorkflowState.COMPLETED
            return context

        try:
            # Step 1: Intake
            context = await self._intake_step(context)
            self._update_phase(context, "design_plan", "in_progress", "Analyzing request and planning architecture.")

            # Step 2: NLU Parsing - understand EXACTLY what user wants
            context = await self._nlu_step(context, request_data.get("aws_region"))
            context = self._apply_input_variables(context, input_variables)
            context = self._apply_resource_selection_to_intent(context)

            prerequisite_questions = await self._collect_prerequisite_questions(
                intent=context.intent or {},
                request_data=request_data,
                input_variables=input_variables,
                request_text=context.natural_language_request,
            )
            if prerequisite_questions:
                self._update_phase(context, "design_plan", "completed", "Design and prerequisite planning completed.")
                self._update_phase(context, "networking_security", "needs_input", "Waiting for required provisioning inputs.")
                self._skip_remaining_phases(context, "compute_data", "Waiting for user-selected prerequisites.")
                context.execution_result = {
                    "success": False,
                    "requires_input": True,
                    "question_prompt": "I need a few required inputs before provisioning.",
                    "questions": prerequisite_questions,
                }
                self._attach_phases_to_result(context)
                context.current_state = WorkflowState.COMPLETED
                return context

            # Step 3: Build context and tags
            context = await self._context_build_step(context)

            # Step 4: RAG retrieval for reference docs
            context = await self._rag_retrieval_step(context)

            # Step 5: Policy check
            context = await self._policy_check_step(context)
            self._update_phase(context, "design_plan", "completed", "Design and plan phase completed.")

            # Step 6: Execute in AWS
            aws_access_key = request_data.get("aws_access_key") or input_variables.get("aws_access_key")
            aws_secret_key = request_data.get("aws_secret_key") or input_variables.get("aws_secret_key")
            aws_region = request_data.get("aws_region") or input_variables.get("aws_region")

            if aws_access_key and aws_secret_key:
                region = context.intent.get("region") if context.intent else request_data.get("aws_region", "us-west-2")
                self.aws_executor.initialize(
                    aws_access_key,
                    aws_secret_key,
                    region or aws_region or "us-west-2",
                )
                context = await self._aws_execution_step(context)
            else:
                context.execution_result = {
                    "success": False,
                    "error": "AWS credentials not provided. Please provide Access Key and Secret Key.",
                }
                self._update_phase(context, "networking_security", "failed", "Missing AWS credentials.")
                self._skip_remaining_phases(context, "compute_data", "Waiting for required credentials.")

            context = self._attach_auto_remediation(context)
            context = self._attach_clarification_questions(context)
            context = self._attach_success_followup_questions(context)
            self._attach_resume_context(context)
            self._attach_phases_to_result(context)

            context.current_state = WorkflowState.COMPLETED

        except Exception as e:
            context.current_state = WorkflowState.FAILED
            context.error = str(e)
            self._fail_active_phase(context, str(e))
            self._attach_phases_to_result(context)
            print(f"  Orchestration failed: {e}")

        return context

    async def _resume_skipped_stage(
        self,
        context: WorkflowContext,
        request_data: Dict[str, Any],
        input_variables: Dict[str, Any],
        resume_context: Dict[str, Any],
    ) -> WorkflowContext:
        context.current_state = WorkflowState.AWS_EXECUTION
        restored_intent = copy.deepcopy(resume_context.get("intent") or {})
        context.intent = restored_intent if isinstance(restored_intent, dict) else {}
        context = self._apply_input_variables(context, input_variables)

        restored_phases = resume_context.get("phases")
        if isinstance(restored_phases, list) and restored_phases:
            context.context_data["phases"] = copy.deepcopy(restored_phases)
        else:
            self._initialize_phases(context)

        phases = context.context_data.get("phases", [])
        target_phase = None
        for phase in phases:
            if phase.get("status") in {"failed", "skipped", "needs_input", "pending", "in_progress"}:
                target_phase = phase
                break
        if not target_phase:
            context.execution_result = {
                "success": True,
                "message": "No skipped/failed stages found to resume.",
            }
            return context

        target_phase_id = target_phase.get("id")
        if target_phase_id == "deploy_app":
            context = await self._resume_deploy_stage(context, request_data, resume_context)
            return context

        error_text = ""
        previous_result = resume_context.get("execution_result")
        if isinstance(previous_result, dict):
            error_text = str(previous_result.get("error") or "")
        questions = self._build_questions_from_errors([error_text] if error_text else [], context.intent or {})
        if not questions:
            questions = [
                {
                    "variable": "resume_confirmation",
                    "question": f"Resuming stage '{target_phase_id}' requires additional details. Describe what to fix and continue.",
                    "type": "string",
                }
            ]
        context.execution_result = {
            "success": False,
            "requires_input": True,
            "question_prompt": f"Resume requested for stage '{target_phase_id}'. Provide required details to continue.",
            "questions": questions,
            "resumed_from": target_phase_id,
        }
        self._update_phase(context, target_phase_id, "needs_input", "Waiting for details to resume skipped stage.")
        return context

    async def _resume_deploy_stage(
        self,
        context: WorkflowContext,
        request_data: Dict[str, Any],
        resume_context: Dict[str, Any],
    ) -> WorkflowContext:
        intent = context.intent or {}
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        resource_type = str(intent.get("resource_type", "")).lower().strip()
        if resource_type != "ec2":
            context.execution_result = {
                "success": False,
                "requires_input": True,
                "question_prompt": "Deploy-stage resume is currently automated for EC2 targets. Provide custom instructions to continue.",
                "questions": [
                    {
                        "variable": "custom_instruction",
                        "question": "What deployment action should I run for this resource?",
                        "type": "string",
                    }
                ],
            }
            self._update_phase(context, "deploy_app", "needs_input", "Waiting for deploy instructions.")
            return context

        prior_result = resume_context.get("execution_result", {}) if isinstance(resume_context.get("execution_result"), dict) else {}
        instance_id = (
            params.get("instance_id")
            or prior_result.get("instance_id")
            or resume_context.get("instance_id")
        )
        if not instance_id:
            context.execution_result = {
                "success": False,
                "requires_input": True,
                "question_prompt": "I need EC2 instance ID to resume deploy stage.",
                "questions": [
                    {"variable": "instance_id", "question": "Provide EC2 instance ID.", "type": "string"}
                ],
            }
            self._update_phase(context, "deploy_app", "needs_input", "Waiting for target instance ID.")
            return context

        app_targets = params.get("app_targets") or params.get("install_targets")
        if isinstance(app_targets, str):
            app_targets = [x.strip() for x in app_targets.split(",") if x.strip()]
        if not app_targets:
            context.execution_result = {
                "success": False,
                "requires_input": True,
                "question_prompt": "Resuming deploy stage. Please provide app deployment details.",
                "questions": [
                    {
                        "variable": "app_targets",
                        "question": "What applications/tools should I install?",
                        "type": "string",
                        "hint": "Example: tomcat,nginx,docker",
                        "options": ["tomcat", "nginx", "docker", "node", "python", "java", "git"],
                    },
                    {
                        "variable": "app_port",
                        "question": "Which application port should I open?",
                        "type": "number",
                        "hint": "Example: 8080",
                    },
                    {
                        "variable": "public_access",
                        "question": "Should this app port be public? (true/false)",
                        "type": "boolean",
                        "options": ["true", "false"],
                    },
                ],
                "resumed_from": "deploy_app",
            }
            self._update_phase(context, "deploy_app", "needs_input", "Waiting for deployment details.")
            return context

        aws_access_key = request_data.get("aws_access_key") or params.get("aws_access_key")
        aws_secret_key = request_data.get("aws_secret_key") or params.get("aws_secret_key")
        aws_region = intent.get("region") or request_data.get("aws_region") or "us-east-1"
        if not (aws_access_key and aws_secret_key):
            context.execution_result = {
                "success": False,
                "requires_input": True,
                "question_prompt": "AWS credentials are required to resume deploy stage.",
                "questions": [
                    {"variable": "aws_access_key", "question": "Provide AWS Access Key ID.", "type": "string"},
                    {"variable": "aws_secret_key", "question": "Provide AWS Secret Access Key.", "type": "password"},
                ],
            }
            self._update_phase(context, "deploy_app", "needs_input", "Missing AWS credentials for resume.")
            return context

        self.aws_executor.initialize(aws_access_key, aws_secret_key, aws_region)
        self._update_phase(context, "deploy_app", "in_progress", "Resuming deployment on existing instance.")
        deploy_result = await self.aws_executor.deploy_to_ec2_via_ssm(
            instance_id=str(instance_id),
            app_targets=app_targets,
            app_port=params.get("app_port"),
            public_access=bool(params.get("public_access", True)),
            custom_commands=params.get("custom_commands"),
            wait_seconds=int(params.get("wait_seconds", 300) or 300),
        )
        context.execution_result = deploy_result
        if deploy_result.get("success"):
            self._update_phase(context, "deploy_app", "completed", "Deployment resumed and completed successfully.")
            self._update_phase(context, "validate_health", "completed", "Validation completed after resume.")
        elif deploy_result.get("requires_input"):
            self._update_phase(context, "deploy_app", "needs_input", "More deployment inputs required.")
        else:
            self._update_phase(context, "deploy_app", "failed", deploy_result.get("error") or "Deployment resume failed.")
            self._skip_remaining_phases(context, "validate_health", "Skipped after deploy stage failure.")
        context.execution_result.setdefault("resumed_from", "deploy_app")
        return context

    async def execute_remediation_with_resume(
        self,
        remediation_run: Dict[str, Any],
        approved: bool,
    ) -> Dict[str, Any]:
        if not isinstance(remediation_run, dict):
            return {"success": False, "error": "Invalid remediation context."}
        if not approved:
            return {
                "success": False,
                "requires_input": False,
                "status": "denied",
                "message": "Remediation approval denied. Execution paused until manual fix is applied.",
            }

        plan = remediation_run.get("plan", {}) if isinstance(remediation_run.get("plan"), dict) else {}
        request_snapshot = remediation_run.get("request_snapshot", {}) if isinstance(remediation_run.get("request_snapshot"), dict) else {}
        resume_context = remediation_run.get("resume_context", {}) if isinstance(remediation_run.get("resume_context"), dict) else {}
        aws_access_key = request_snapshot.get("aws_access_key")
        aws_secret_key = request_snapshot.get("aws_secret_key")
        aws_region = request_snapshot.get("aws_region") or "us-east-1"
        environment = request_snapshot.get("environment") or "dev"
        if not aws_access_key or not aws_secret_key:
            return {"success": False, "error": "AWS credentials missing for remediation execution."}

        self.aws_executor.initialize(aws_access_key, aws_secret_key, aws_region)
        rem_result = await self.remediation_engine.execute_plan(
            remediation_plan=plan,
            aws_executor=self.aws_executor,
            auth_context={"environment": environment},
        )
        if not rem_result.get("success"):
            return {
                "success": False,
                "error": rem_result.get("error") or "Remediation execution failed.",
                "remediation_result": rem_result,
            }

        if not resume_context:
            return {
                "success": True,
                "remediation_result": rem_result,
                "message": "Remediation applied. No resume context available; please retry the request.",
            }

        resumed_request = {
            "request_id": request_snapshot.get("request_id") or f"req-{uuid.uuid4().hex[:12]}",
            "requester_id": request_snapshot.get("requester_id") or "user",
            "environment": environment,
            "cloud_provider": request_snapshot.get("cloud_provider") or "aws",
            "natural_language_request": request_snapshot.get("natural_language_request") or "resume",
            "aws_access_key": aws_access_key,
            "aws_secret_key": aws_secret_key,
            "aws_region": aws_region,
            "input_variables": {
                "resume_skipped_only": True,
                "resume_context": resume_context,
            },
        }

        resumed_context = await self.process_request(resumed_request)
        return {
            "success": bool(
                isinstance(resumed_context.execution_result, dict)
                and resumed_context.execution_result.get("success")
            ),
            "workflow_state": resumed_context.current_state.value,
            "intent": resumed_context.intent,
            "execution_result": resumed_context.execution_result,
            "error": resumed_context.error,
            "remediation_result": rem_result,
        }

    async def _attempt_self_heal_and_retry(
        self,
        context: WorkflowContext,
        failure_result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not settings.AUTO_REMEDIATION_ENABLED:
            return None
        if not settings.AUTO_REMEDIATION_AUTO_EXECUTE_SAFE:
            return None
        if not isinstance(failure_result, dict):
            return None
        if failure_result.get("success") or failure_result.get("requires_input"):
            return None

        intent = context.intent or {}
        remediation_plan = self.remediation_engine.build_plan(
            intent=intent,
            execution_result=failure_result,
            context={
                "environment": context.environment,
                "region": intent.get("region"),
                "request_id": context.request_id,
            },
        )
        if not remediation_plan:
            return None
        execution_actions = remediation_plan.get("execution_actions", []) if isinstance(remediation_plan, dict) else []
        if not execution_actions:
            return None
        safety = remediation_plan.get("safety", {}) if isinstance(remediation_plan.get("safety"), dict) else {}
        if self._to_bool(safety.get("destructive"), False):
            return None
        if self._to_bool(safety.get("requires_admin"), False):
            return None

        rem_result = await self.remediation_engine.execute_plan(
            remediation_plan=remediation_plan,
            aws_executor=self.aws_executor,
            auth_context={"environment": context.environment},
        )
        if not rem_result.get("success"):
            return None

        retried: Dict[str, Any]
        if self._should_run_rds_sql_executor(intent):
            retried = await self._run_rds_sql_executor(context)
            retried.setdefault("execution_path", "rds_sql_ssm_auto_healed")
        elif self._should_run_service_workflow(intent, context.natural_language_request):
            retried = await self._run_service_workflow(context)
            retried.setdefault("execution_path", "service_workflow_auto_healed")
        else:
            retried = await self.aws_executor.execute(
                action=str(intent.get("action") or "create"),
                resource_type=str(intent.get("resource_type") or "unknown"),
                resource_name=intent.get("resource_name"),
                parameters=intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {},
                tags=context.context_data.get("required_tags", {}),
            )
            retried.setdefault("execution_path", "static_auto_healed")

        if not isinstance(retried, dict):
            return None
        retried["auto_healed"] = True
        retried["auto_heal_plan"] = {
            "rule_id": remediation_plan.get("rule_id"),
            "run_id": remediation_plan.get("run_id"),
        }
        retried["auto_heal_result"] = rem_result
        retried["previous_failure"] = failure_result
        return retried

    def _attach_auto_remediation(self, context: WorkflowContext) -> WorkflowContext:
        result = context.execution_result if isinstance(context.execution_result, dict) else None
        if not result:
            return context
        if result.get("success"):
            return context
        if result.get("requires_input") and result.get("remediation"):
            return context
        if not settings.AUTO_REMEDIATION_ENABLED:
            return context
        if settings.AUTO_REMEDIATION_AUTO_EXECUTE_SAFE:
            # Auto-execution path already attempted in AWS execution step.
            return context

        intent = context.intent or {}
        resource_type = str(intent.get("resource_type") or "").strip().lower()
        if not settings.AUTO_REMEDIATION_ALL_SERVICES and resource_type not in {"ec2", "rds"}:
            return context
        remediation_plan = self.remediation_engine.build_plan(
            intent=intent,
            execution_result=result,
            context={
                "environment": context.environment,
                "region": intent.get("region"),
                "request_id": context.request_id,
            },
        )
        if not remediation_plan:
            return context

        result["requires_input"] = True
        result["question_prompt"] = remediation_plan.get("reason") or "I found a prerequisite issue I can auto-fix."
        result["questions"] = [
            {
                "variable": "remediation_approval",
                "question": "Approve automatic remediation and continue? (approve/deny)",
                "type": "string",
                "options": ["approve", "deny"],
                "hint": "approve applies safe auto-fix then retries failed stage",
            }
        ]
        result["continuation"] = {
            "kind": "auto_remediation",
            "run_id": remediation_plan.get("run_id"),
            "approval_scope": remediation_plan.get("approval_scope", "request_run"),
            "required_permissions": remediation_plan.get("required_permissions", []),
        }
        result["remediation"] = remediation_plan

        phases = context.context_data.get("phases", [])
        target_phase_id = "deploy_app"
        for phase in phases:
            if phase.get("status") in {"failed", "in_progress"}:
                target_phase_id = phase.get("id") or target_phase_id
                break
        self._update_phase(context, target_phase_id, "needs_input", "Waiting for remediation approval.")
        context.execution_result = result
        return context

    def _attach_resume_context(self, context: WorkflowContext):
        result = context.execution_result if isinstance(context.execution_result, dict) else None
        if not result:
            return
        phases = context.context_data.get("phases", [])
        skipped = [p for p in phases if p.get("status") in {"failed", "skipped", "needs_input"}]
        if not skipped:
            result.pop("resume_context", None)
            return
        next_phase = skipped[0].get("id")
        snapshot = {k: v for k, v in result.items() if k not in {"resume_context", "resume_available"}}
        result["resume_context"] = {
            "intent": copy.deepcopy(context.intent or {}),
            "phases": copy.deepcopy(phases),
            "execution_result": copy.deepcopy(snapshot),
            "next_phase": next_phase,
        }
        if isinstance(result.get("remediation"), dict):
            result["resume_context"]["remediation_context"] = copy.deepcopy(result.get("remediation"))
        result["resume_available"] = True

    async def improve_prompt(
        self,
        natural_language_request: str,
        environment: str = "dev",
        aws_region: Optional[str] = None,
    ) -> Dict[str, Any]:
        text = (natural_language_request or "").strip()
        if not text:
            return {
                "original_prompt": natural_language_request,
                "improved_prompt": "",
                "summary": "Please type what you want to build.",
                "phase_plan": self._default_phase_plan(),
                "intent_preview": None,
            }

        intent = await self.nlu_service.parse_request(text, user_region=aws_region)
        improved_prompt = self._compose_improved_prompt(text, intent, environment, aws_region or intent.region)
        return {
            "original_prompt": text,
            "improved_prompt": improved_prompt,
            "summary": "I rewrote your request into a clearer execution prompt. Review and confirm before running.",
            "phase_plan": self._default_phase_plan(),
            "intent_preview": intent.to_dict(),
            "is_complex": self._is_complex_request(text),
        }

    async def _intake_step(self, context: WorkflowContext) -> WorkflowContext:
        print(f"  Step 1 - Intake: {context.request_id}")
        context.current_state = WorkflowState.INTAKE
        context.history.append({"step": "intake", "timestamp": datetime.utcnow().isoformat()})
        return context

    async def _nlu_step(self, context: WorkflowContext, user_region: Optional[str] = None) -> WorkflowContext:
        print(f"  Step 2 - NLU Parsing")
        context.current_state = WorkflowState.NLU_PARSING

        intent = await self.nlu_service.parse_request(
            context.natural_language_request,
            user_region=user_region,
        )
        context.intent = intent.to_dict()

        print(f"  Intent: action={intent.action}, type={intent.resource_type}, name={intent.resource_name}")
        context.history.append({
            "step": "nlu_parsing",
            "intent": context.intent,
            "timestamp": datetime.utcnow().isoformat(),
        })
        return context

    async def _context_build_step(self, context: WorkflowContext) -> WorkflowContext:
        print(f"  Step 3 - Context Build")
        context.current_state = WorkflowState.CONTEXT_BUILD

        context.context_data["required_tags"] = {
            "Environment": context.environment,
            "ManagedBy": "AI-Platform",
            "CreatedBy": context.requester_id,
            "RequestId": context.request_id,
        }

        # Add resource name as tag if available
        if context.intent and context.intent.get("resource_name"):
            context.context_data["required_tags"]["Name"] = context.intent["resource_name"]

        return context

    async def _rag_retrieval_step(self, context: WorkflowContext) -> WorkflowContext:
        print(f"  Step 4 - RAG Retrieval")
        context.current_state = WorkflowState.RAG_RETRIEVAL
        try:
            resource_type = context.intent.get("resource_type", "") if context.intent else ""
            docs = await self.rag_service.retrieve(
                query=f"{context.cloud_provider} {resource_type} {context.natural_language_request}",
                n_results=3,
            )
            context.context_data["retrieved_docs"] = docs
        except Exception:
            context.context_data["retrieved_docs"] = []
        return context

    async def _policy_check_step(self, context: WorkflowContext) -> WorkflowContext:
        print(f"  Step 5 - Policy Check")
        context.current_state = WorkflowState.POLICY_CHECK

        intent = context.intent or {}
        action = intent.get("action", "create")
        resource_type = intent.get("resource_type", "unknown")

        # Production environment restrictions
        violations = []
        if context.environment == "prod":
            if action == "delete":
                violations.append("Delete operations in production require manual approval")
            if resource_type in ("vpc", "rds", "eks") and action == "create":
                violations.append(f"Creating {resource_type} in production requires additional review")

        context.context_data["policy"] = {
            "passed": len(violations) == 0,
            "violations": violations,
        }

        if violations:
            context.execution_result = {
                "success": False,
                "error": f"Policy violations: {'; '.join(violations)}",
            }

        return context

    async def _aws_execution_step(self, context: WorkflowContext) -> WorkflowContext:
        """Execute the parsed intent against AWS using dynamic Boto3 codegen."""
        print(f"  Step 6 - AWS Execution (Dynamic Boto3)")
        context.current_state = WorkflowState.AWS_EXECUTION
        self._update_phase(context, "networking_security", "in_progress", "Applying networking and security controls.")
        self._update_phase(context, "compute_data", "pending", "Waiting for networking/security completion.")
        self._update_phase(context, "deploy_app", "pending", "Waiting for compute/data provisioning.")
        self._update_phase(context, "validate_health", "pending", "Waiting for deployment.")

        # Skip if policy check failed
        if context.context_data.get("policy", {}).get("passed") is False:
            self._update_phase(context, "networking_security", "failed", "Policy check failed.")
            self._skip_remaining_phases(context, "compute_data", "Blocked by policy.")
            return context

        try:
            intent = context.intent or {}
            action = intent.get("action", "create")
            resource_type = intent.get("resource_type", "unknown")
            if intent.get("parameters"):
                context.intent["parameters"] = self._sanitize_intent_parameters(intent["parameters"])
            params = context.intent.get("parameters", {}) if isinstance(context.intent.get("parameters"), dict) else {}
            prefill = await self.aws_executor.auto_fill_intent_prerequisites(
                action=action,
                resource_type=resource_type,
                resource_name=intent.get("resource_name"),
                parameters=params,
                tags=context.context_data.get("required_tags", {}),
                environment=context.environment,
            )
            if prefill.get("success") and isinstance(prefill.get("parameters"), dict):
                context.intent["parameters"] = self._sanitize_intent_parameters(prefill.get("parameters", {}))
                params = context.intent.get("parameters", {})
            if str(params.get("execution_mode", "")).strip().lower() == "static":
                context.execution_result = await self._execute_static_fallback(
                    context=context,
                    dynamic_result={"success": False, "error": "Dynamic execution skipped by requested static mode."},
                )
                if context.execution_result.get("success"):
                    context.execution_result = await self._enrich_with_outcome_validation(context, context.execution_result)
                    self._finalize_phases_for_success(context, context.execution_result)
                else:
                    self._finalize_phases_for_failure(context, context.execution_result.get("error"))
                return context
            if self._should_run_rds_sql_executor(context.intent):
                result = await self._run_rds_sql_executor(context)
                context.execution_result = result
                if result.get("success"):
                    context.execution_result.setdefault("execution_path", "rds_sql_ssm")
                    context.execution_result = await self._enrich_with_outcome_validation(context, context.execution_result)
                    self._finalize_phases_for_success(context, context.execution_result)
                elif result.get("requires_input"):
                    self._update_phase(context, "networking_security", "needs_input", result.get("question_prompt") or "Waiting for SQL execution inputs.")
                    self._skip_remaining_phases(context, "compute_data", "Waiting for user input before SQL execution.")
                else:
                    healed = await self._attempt_self_heal_and_retry(context, result)
                    if isinstance(healed, dict) and healed.get("success"):
                        healed = await self._enrich_with_outcome_validation(context, healed)
                        context.execution_result = healed
                        self._finalize_phases_for_success(context, healed)
                    elif isinstance(healed, dict) and healed.get("requires_input"):
                        context.execution_result = healed
                        self._update_phase(context, "networking_security", "needs_input", healed.get("question_prompt") or "Waiting for required inputs.")
                        self._skip_remaining_phases(context, "compute_data", "Waiting for user input.")
                    else:
                        self._finalize_phases_for_failure(context, result.get("error"))
                return context
            if self._should_run_service_workflow(context.intent, context.natural_language_request):
                result = await self._run_service_workflow(context)
                context.execution_result = result
                if result.get("success"):
                    context.execution_result.setdefault("execution_path", "service_workflow")
                    context.execution_result = await self._enrich_with_outcome_validation(context, context.execution_result)
                    self._finalize_phases_for_success(context, context.execution_result)
                elif result.get("requires_input"):
                    self._update_phase(context, "networking_security", "needs_input", result.get("question_prompt") or "Waiting for required workflow inputs.")
                    self._skip_remaining_phases(context, "compute_data", "Waiting for workflow input.")
                else:
                    healed = await self._attempt_self_heal_and_retry(context, result)
                    if isinstance(healed, dict) and healed.get("success"):
                        healed = await self._enrich_with_outcome_validation(context, healed)
                        context.execution_result = healed
                        self._finalize_phases_for_success(context, healed)
                    elif isinstance(healed, dict) and healed.get("requires_input"):
                        context.execution_result = healed
                        self._update_phase(context, "networking_security", "needs_input", healed.get("question_prompt") or "Waiting for required inputs.")
                        self._skip_remaining_phases(context, "compute_data", "Waiting for user input.")
                    else:
                        self._finalize_phases_for_failure(context, result.get("error"))
                return context
            codegen_context = self._build_codegen_context(context)

            # 1. Generate dynamic Boto3 code
            code = await self.boto_codegen.generate_code(
                prompt=context.natural_language_request,
                context=codegen_context
            )
            
            if not code:
                context.execution_result = await self._execute_static_fallback(
                    context=context,
                    dynamic_result={"success": False, "error": "Failed to generate automation code"},
                )
                if context.execution_result.get("success"):
                    context.execution_result = await self._enrich_with_outcome_validation(context, context.execution_result)
                    self._finalize_phases_for_success(context, context.execution_result)
                else:
                    self._finalize_phases_for_failure(context, context.execution_result.get("error"))
                return context
            
            context.generated_code = code
            print(f"  Generated Code:\n{code[:200]}...")

            # 2. Execute directly via Boto3
            result = await self.aws_executor.run_dynamic_boto(code)
            if result.get("success"):
                result.setdefault("execution_path", "dynamic")
                result = await self._enrich_with_outcome_validation(context, result)
                context.execution_result = result
                self._finalize_phases_for_success(context, result)
                return context

            # 3. Try dynamic repair pass using first failure signal
            repaired_result = None
            repaired_code = await self.boto_codegen.repair_code(
                prompt=context.natural_language_request,
                context=codegen_context,
                failed_code=code,
                error=result.get("error"),
                output=result.get("output"),
            )
            if repaired_code:
                repaired_result = await self.aws_executor.run_dynamic_boto(repaired_code)
                if repaired_result.get("success"):
                    repaired_result["execution_path"] = "dynamic_repair"
                    repaired_result["previous_error"] = result.get("error")
                    repaired_result = await self._enrich_with_outcome_validation(context, repaired_result)
                    context.execution_result = repaired_result
                    self._finalize_phases_for_success(context, repaired_result)
                    return context

            dynamic_failure = {
                "success": False,
                "error": result.get("error") or "Dynamic execution failed",
                "initial_result": result,
            }
            if repaired_result is not None:
                dynamic_failure["repair_result"] = repaired_result
                dynamic_failure["error"] = repaired_result.get("error") or dynamic_failure["error"]

            # 4. Model-driven generic fallback (botocore operation introspection).
            model_result = await self.aws_executor.execute_model_driven(
                action=action,
                resource_type=resource_type,
                resource_name=intent.get("resource_name"),
                parameters=context.intent.get("parameters", {}) if isinstance(context.intent.get("parameters"), dict) else {},
                tags=context.context_data.get("required_tags", {}),
            )
            if model_result.get("success"):
                model_result["dynamic_result"] = dynamic_failure
                model_result = await self._enrich_with_outcome_validation(context, model_result)
                context.execution_result = model_result
                self._finalize_phases_for_success(context, model_result)
                return context
            if model_result.get("requires_input"):
                model_result["dynamic_result"] = dynamic_failure
                context.execution_result = model_result
                self._update_phase(context, "networking_security", "needs_input", model_result.get("question_prompt") or "Waiting for required inputs.")
                self._skip_remaining_phases(context, "compute_data", "Waiting for missing values before execution.")
                return context
            dynamic_failure["model_result"] = model_result

            # 5. Fallback to static handlers when available
            static_handler_name = f"_{action}_{resource_type}"
            if getattr(self.aws_executor, static_handler_name, None):
                context.execution_result = await self._execute_static_fallback(
                    context=context,
                    dynamic_result=dynamic_failure,
                )
                if context.execution_result.get("success"):
                    context.execution_result = await self._enrich_with_outcome_validation(context, context.execution_result)
                    self._finalize_phases_for_success(context, context.execution_result)
                else:
                    reconciled_result = await self._reconcile_post_failure_create(
                        context=context,
                        failure_payload=context.execution_result,
                    )
                    if reconciled_result:
                        reconciled_result = await self._enrich_with_outcome_validation(context, reconciled_result)
                        context.execution_result = reconciled_result
                        self._finalize_phases_for_success(context, reconciled_result)
                    else:
                        healed = await self._attempt_self_heal_and_retry(context, context.execution_result)
                        if isinstance(healed, dict) and healed.get("success"):
                            healed = await self._enrich_with_outcome_validation(context, healed)
                            context.execution_result = healed
                            self._finalize_phases_for_success(context, healed)
                        elif isinstance(healed, dict) and healed.get("requires_input"):
                            context.execution_result = healed
                            self._update_phase(context, "networking_security", "needs_input", healed.get("question_prompt") or "Waiting for required inputs.")
                            self._skip_remaining_phases(context, "compute_data", "Waiting for user input.")
                        else:
                            self._finalize_phases_for_failure(context, context.execution_result.get("error"))
            else:
                reconciled_result = await self._reconcile_post_failure_create(
                    context=context,
                    failure_payload=dynamic_failure,
                )
                if reconciled_result:
                    reconciled_result = await self._enrich_with_outcome_validation(context, reconciled_result)
                    context.execution_result = reconciled_result
                    self._finalize_phases_for_success(context, reconciled_result)
                else:
                    healed = await self._attempt_self_heal_and_retry(context, dynamic_failure)
                    if isinstance(healed, dict):
                        context.execution_result = healed
                        if healed.get("success"):
                            context.execution_result = await self._enrich_with_outcome_validation(context, healed)
                            self._finalize_phases_for_success(context, context.execution_result)
                        elif healed.get("requires_input"):
                            self._update_phase(context, "networking_security", "needs_input", healed.get("question_prompt") or "Waiting for required inputs.")
                            self._skip_remaining_phases(context, "compute_data", "Waiting for user input.")
                        else:
                            self._finalize_phases_for_failure(context, healed.get("error"))
                    else:
                        context.execution_result = dynamic_failure
                        self._finalize_phases_for_failure(context, dynamic_failure.get("error"))
            
        except Exception as e:
            context.execution_result = {"success": False, "error": str(e)}
            self._finalize_phases_for_failure(context, str(e))

        return context

    async def _enrich_with_outcome_validation(self, context: WorkflowContext, result: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(result, dict) or not result.get("success"):
            return result
        validation = await self.outcome_registry.validate(context.intent or {}, result)
        if validation.get("performed"):
            summary = validation.get("summary", {}) if isinstance(validation.get("summary"), dict) else {}
            failed = int(summary.get("failed", 0) or 0)
            pending = int(summary.get("pending", 0) or 0)
            result["outcome_validation"] = validation
            if failed == 0 and pending == 0:
                result["final_outcome"] = "Outcome validation passed."
            elif failed == 0 and pending > 0:
                result["final_outcome"] = f"Provisioning completed. Validation is still in progress ({pending} pending checks)."
            else:
                result["final_outcome"] = f"Provisioning completed with {failed} validation check(s) failing."
        return result

    async def _reconcile_post_failure_create(
        self,
        context: WorkflowContext,
        failure_payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        intent = context.intent or {}
        action = str(intent.get("action", "")).lower().strip()
        resource_type = str(intent.get("resource_type", "")).lower().strip()
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        if action != "create":
            return None

        if resource_type == "rds":
            identifier = params.get("db_instance_id") or params.get("DBInstanceIdentifier") or intent.get("resource_name")
            if not identifier:
                return None
            describe = await self.aws_executor.execute(
                action="describe",
                resource_type="rds",
                resource_name=str(identifier),
                parameters={},
                tags={},
            )
            if describe.get("success"):
                return {
                    "success": True,
                    "action": "create",
                    "resource_type": "rds",
                    "db_instance_id": describe.get("db_instance_id") or str(identifier),
                    "engine": describe.get("engine"),
                    "instance_class": describe.get("class"),
                    "storage_gb": describe.get("storage_gb"),
                    "status": describe.get("status"),
                    "endpoint": describe.get("endpoint"),
                    "port": describe.get("port"),
                    "region": intent.get("region"),
                    "execution_path": "reconciled_post_failure",
                    "recovered_after_failure": True,
                    "previous_failure": failure_payload,
                }

        if resource_type == "lambda":
            identifier = intent.get("resource_name") or params.get("function_name")
            if not identifier:
                return None
            describe = await self.aws_executor.execute(
                action="describe",
                resource_type="lambda",
                resource_name=str(identifier),
                parameters={},
                tags={},
            )
            if describe.get("success"):
                return {
                    "success": True,
                    "action": "create",
                    "resource_type": "lambda",
                    "function_name": describe.get("function_name") or str(identifier),
                    "arn": describe.get("arn"),
                    "runtime": describe.get("runtime"),
                    "memory": describe.get("memory"),
                    "timeout": describe.get("timeout"),
                    "state": describe.get("state"),
                    "region": intent.get("region"),
                    "execution_path": "reconciled_post_failure",
                    "recovered_after_failure": True,
                    "previous_failure": failure_payload,
                }
        return None

    async def _execute_static_fallback(self, context: WorkflowContext, dynamic_result: Dict[str, Any]) -> Dict[str, Any]:
        """Fallback path when dynamic codegen/execution fails."""
        intent = context.intent or {}
        action = intent.get("action", "create")
        resource_type = intent.get("resource_type", "unknown")
        resource_name = intent.get("resource_name")
        parameters = intent.get("parameters", {})
        tags = context.context_data.get("required_tags", {})

        static_result = await self.aws_executor.execute(
            action=action,
            resource_type=resource_type,
            resource_name=resource_name,
            parameters=parameters,
            tags=tags,
        )

        if static_result.get("success"):
            static_result["execution_path"] = "static_fallback"
            static_result["dynamic_result"] = dynamic_result
            return static_result

        return {
            "success": False,
            "error": static_result.get("error") or dynamic_result.get("error") or "Execution failed in dynamic and static paths.",
            "dynamic_result": dynamic_result,
            "static_result": static_result,
        }

    def _sanitize_intent_parameters(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(parameters, dict):
            return {}
        placeholders = {"", "null", "none", "default", "n/a", "na", "auto-generated"}
        sanitized = {}
        key_aliases = {
            "instancetype": "instance_type",
            "keyname": "key_name",
            "imageid": "ami_id",
            "vpcid": "vpc_id",
            "subnetid": "subnet_id",
            "subnetids": "subnet_ids",
            "userdata": "user_data",
            "publiclyaccessible": "public_access",
            "publicip": "public_access",
            "vpcsecuritygroupids": "vpc_security_group_ids",
            "securitygroupids": "security_group_ids",
            "dbinstanceclass": "instance_type",
            "allocatedstorage": "storage_size",
            "masterusername": "master_username",
            "masteruserpassword": "master_password",
            "engineversion": "engine_version",
            "releaselabel": "release_label",
            "masterinstancetype": "master_instance_type",
            "slaveinstancetype": "worker_instance_type",
            "workerinstancetype": "worker_instance_type",
            "instancecount": "instance_count",
            "rolearn": "role_arn",
            "iaminstanceprofile": "iam_instance_profile",
            "cidrblock": "cidr_block",
            "dbname": "db_name",
            "dbinstanceidentifier": "db_instance_id",
            "clusteridentifier": "cluster_id",
            "clustername": "cluster_name",
            "functionname": "function_name",
            "memorysize": "memory",
            "workloadid": "workload_id",
            "workloadname": "workload_name",
            "existingresourceid": "existing_resource_id",
            "existingoperation": "existing_operation",
            "resourceselection": "resource_strategy",
            "resourcestrategy": "resource_strategy",
            "custominstruction": "custom_instruction",
            "websiteconfiguration": "website_configuration",
            "websiteenabled": "website_enabled",
            "indexdocument": "index_document",
            "errordocument": "error_document",
            "indexcontent": "index_content",
            "createindexfile": "create_index_file",
            "clientrequesttoken": "client_request_token",
            "accountids": "account_ids",
            "awsregions": "aws_regions",
            "nonawsregions": "non_aws_regions",
            "pillarpriorities": "pillar_priorities",
            "architecturaldesign": "architectural_design",
            "reviewowner": "review_owner",
            "isreviewownerupdateacknowledged": "is_review_owner_update_acknowledged",
            "industrytype": "industry_type",
            "improvementstatus": "improvement_status",
            "discoveryconfig": "discovery_config",
            "jiraconfiguration": "jira_configuration",
            "profilearns": "profile_arns",
            "reviewtemplatearns": "review_template_arns",
            "dbusername": "db_username",
            "dbuser": "db_username",
            "dbpassword": "db_password",
            "bastioninstanceid": "bastion_instance_id",
            "secretarn": "secret_arn",
            "sql": "sql",
            "sqlstatements": "sql_statements",
            "ensurenetworkpath": "ensure_network_path",
            "runsqlviassm": "run_sql_via_ssm",
            "storagesize": "storage_size",
            "volumesize": "storage_size",
            "os": "os_flavor",
            "osflavor": "os_flavor",
            "servicetargets": "service_targets",
        }
        drop_keys = {
            "tagspecifications",
            "monitoringconfiguration",
            "blockdevicemappings",
            "networkinterfaces",
            "groups",
            "tags",
        }
        list_like_keys = {
            "subnet_ids",
            "security_group_ids",
            "vpc_security_group_ids",
            "app_targets",
            "install_targets",
            "lenses",
            "account_ids",
            "aws_regions",
            "non_aws_regions",
            "pillar_priorities",
            "applications",
            "profile_arns",
            "review_template_arns",
            "service_targets",
        }
        for key, value in parameters.items():
            normalized_key = "".join(ch for ch in str(key).lower() if ch.isalnum())
            if normalized_key in drop_keys:
                continue
            key = key_aliases.get(normalized_key, key)
            if value is None:
                continue
            if isinstance(value, str):
                cleaned = value.strip().strip("'\"")
                if cleaned.lower() in placeholders:
                    continue
                if cleaned.lower() in {"true", "false"}:
                    sanitized[key] = cleaned.lower() == "true"
                    continue
                if key in list_like_keys and "," in cleaned:
                    sanitized[key] = [segment.strip() for segment in cleaned.split(",") if segment.strip()]
                    continue
                if key == "user_data":
                    lines = cleaned.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                    lines = [line for line in lines if line.strip().lower() not in placeholders]
                    cleaned = "\n".join(lines).strip()
                    if cleaned and not cleaned.startswith("#!/bin/bash"):
                        cleaned = "#!/bin/bash\n" + cleaned
                    if not cleaned:
                        continue
                sanitized[key] = cleaned
                continue
            if isinstance(value, list):
                cleaned_list = []
                for item in value:
                    if item is None:
                        continue
                    if isinstance(item, str):
                        item = item.strip().strip("'\"")
                        if item.lower() in placeholders:
                            continue
                    cleaned_list.append(item)
                if cleaned_list:
                    sanitized[key] = cleaned_list
                continue
            if isinstance(value, dict):
                sanitized[key] = value
                continue
            sanitized[key] = value
        return sanitized

    def _build_codegen_context(self, context: WorkflowContext) -> Dict[str, Any]:
        """Build safe context for code generation without injecting huge shell scripts."""
        safe_intent = copy.deepcopy(context.intent or {})
        params = safe_intent.get("parameters")
        if isinstance(params, dict) and "user_data" in params:
            params["user_data_present"] = bool(params.get("user_data"))
            params["user_data_note"] = "Cloud-init user_data script provided separately at runtime."
            params.pop("user_data", None)

        return {
            "intent": safe_intent,
            "environment": context.environment,
            "tags": context.context_data.get("required_tags", {}),
            "retrieved_docs": context.context_data.get("retrieved_docs", []),
        }

    def _should_run_rds_sql_executor(self, intent: Dict[str, Any]) -> bool:
        if not isinstance(intent, dict):
            return False
        action = str(intent.get("action", "")).lower().strip()
        if str(intent.get("resource_type", "")).lower().strip() != "rds":
            return False
        if action == "create":
            return False
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        if params.get("run_sql_via_ssm"):
            return True
        if str(params.get("existing_operation", "")).strip().lower() == "custom":
            return bool(params.get("sql") or params.get("sql_statements"))
        return bool(params.get("sql") or params.get("sql_statements"))

    def _should_run_service_workflow(self, intent: Dict[str, Any], request_text: str = "") -> bool:
        if not isinstance(intent, dict):
            return False
        action = str(intent.get("action", "")).strip().lower()
        resource = str(intent.get("resource_type", "")).strip().lower()
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        text = str(request_text or "").lower()

        if resource == "eks" and action in {"create", "update"}:
            # Always prefer deterministic service workflow for EKS create/update.
            return True
        if resource in {"s3", "lambda", "rds"} and action in {"create", "update"}:
            if str(params.get("resource_strategy", "")).strip().lower() == "existing":
                return True
            if any(
                token in text
                for token in [
                    "continue",
                    "resume",
                    "status",
                    "what is the update",
                    "auto deploy",
                    "auto-deploy",
                ]
            ):
                return True
            # Make these high-volume services run through deterministic workflow adapters.
            return True
        if action in {"create", "update"} and resource and resource not in {"unknown", "ec2"}:
            # Generic all-service deterministic path when static handlers exist.
            if hasattr(self.aws_executor, f"_{action}_{resource}"):
                return True
        return False

    async def _run_service_workflow(self, context: WorkflowContext) -> Dict[str, Any]:
        intent = context.intent or {}
        resource = str(intent.get("resource_type", "")).strip().lower()
        if resource == "eks":
            return await self._run_eks_service_workflow(context)
        if resource == "s3":
            return await self._run_s3_service_workflow(context)
        if resource == "lambda":
            return await self._run_lambda_service_workflow(context)
        if resource == "rds":
            return await self._run_rds_service_workflow(context)
        return await self._run_generic_service_workflow(context)

    async def _run_eks_service_workflow(self, context: WorkflowContext) -> Dict[str, Any]:
        intent = context.intent or {}
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        request_text = str(context.natural_language_request or "").lower()
        cluster_name = (
            intent.get("resource_name")
            or params.get("existing_resource_id")
            or params.get("cluster_name")
            or params.get("target_resource_id")
        )
        if not cluster_name:
            auto_cluster = await self._auto_select_existing_resource_name(
                resource_type="eks",
                request_text=context.natural_language_request,
                prefer_pending=True,
            )
            if auto_cluster:
                cluster_name = auto_cluster
                params["existing_resource_id"] = auto_cluster
                params["target_resource_id"] = auto_cluster
                intent["resource_name"] = auto_cluster
                intent["parameters"] = params
                context.intent = intent
        if not cluster_name:
            return {
                "success": False,
                "requires_input": True,
                "question_prompt": "I need the EKS cluster name/ID to continue this Kubernetes deployment.",
                "questions": [
                    {
                        "variable": "existing_resource_id",
                        "question": "Provide the existing EKS cluster name.",
                        "type": "string",
                    }
                ],
            }

        workflow_params = dict(params)
        wants_workload = any(
            token in request_text
            for token in [
                "deploy",
                "website",
                "web app",
                "webapp",
                "application",
                "public url",
                "ingress",
                "load balancer",
                "tomcat",
            ]
        )
        workflow_params.setdefault("wait_for_active", True)
        workflow_params.setdefault("wait_for_nodegroup", True)
        workflow_params.setdefault("deploy_sample_website", wants_workload)
        workflow_params.setdefault("expose_public_url", wants_workload)
        workflow_params.setdefault("private_workers", True)

        if "tomcat" in request_text and not workflow_params.get("website_image"):
            workflow_params["website_image"] = "tomcat:10.1-jdk17-temurin"
            workflow_params.setdefault("app_name", "tomcat-app")
            workflow_params.setdefault("namespace", "tomcat-site")

        if workflow_params.get("node_instance_type") in (None, "", []):
            workflow_params["node_instance_type"] = workflow_params.get("instance_type") or "t3.medium"
        if workflow_params.get("node_count") in (None, "", []):
            match = re.search(r"(?:desired|nodes?|node count)\s*[:=]?\s*(\d+)", request_text)
            workflow_params["node_count"] = int(match.group(1)) if match else 2

        result = await self.aws_executor.run_eks_workflow(
            cluster_name=str(cluster_name),
            parameters=workflow_params,
            tags=context.context_data.get("required_tags", {}),
            environment=context.environment,
        )
        result.setdefault("action", "update")
        result.setdefault("resource_type", "eks")
        result.setdefault("cluster_name", str(cluster_name))
        return result

    async def _run_s3_service_workflow(self, context: WorkflowContext) -> Dict[str, Any]:
        intent = context.intent or {}
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        action = str(intent.get("action", "create")).strip().lower() or "create"
        bucket_name = (
            intent.get("resource_name")
            or params.get("existing_resource_id")
            or params.get("bucket_name")
            or params.get("target_resource_id")
        )
        if action == "update" and not bucket_name:
            auto_bucket = await self._auto_select_existing_resource_name(
                resource_type="s3",
                request_text=context.natural_language_request,
                prefer_pending=True,
            )
            if auto_bucket:
                bucket_name = auto_bucket
                params["existing_resource_id"] = auto_bucket
                params["target_resource_id"] = auto_bucket
                intent["resource_name"] = auto_bucket
                intent["parameters"] = params
                context.intent = intent
        if action == "update" and not bucket_name:
            return {
                "success": False,
                "requires_input": True,
                "question_prompt": "I need the S3 bucket name to continue.",
                "questions": [
                    {
                        "variable": "existing_resource_id",
                        "question": "Provide the existing S3 bucket name.",
                        "type": "string",
                    }
                ],
            }

        workflow_params = dict(params)
        website_requested = bool(
            workflow_params.get("website_configuration")
            or workflow_params.get("website_enabled")
            or workflow_params.get("index_document")
            or workflow_params.get("index_content")
        )
        if website_requested:
            workflow_params.setdefault("website_configuration", True)
            workflow_params.setdefault("create_index_file", True)
            workflow_params.setdefault("public_access", True)

        result = await self.aws_executor.run_s3_workflow(
            action=action,
            bucket_name=str(bucket_name) if bucket_name else None,
            parameters=workflow_params,
            tags=context.context_data.get("required_tags", {}),
            environment=context.environment,
        )
        result.setdefault("action", action)
        result.setdefault("resource_type", "s3")
        if bucket_name:
            result.setdefault("bucket_name", str(bucket_name))
        return result

    async def _run_lambda_service_workflow(self, context: WorkflowContext) -> Dict[str, Any]:
        intent = context.intent or {}
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        action = str(intent.get("action", "create")).strip().lower() or "create"
        function_name = (
            intent.get("resource_name")
            or params.get("existing_resource_id")
            or params.get("function_name")
            or params.get("target_resource_id")
        )
        if action == "update" and not function_name:
            auto_function = await self._auto_select_existing_resource_name(
                resource_type="lambda",
                request_text=context.natural_language_request,
                prefer_pending=True,
            )
            if auto_function:
                function_name = auto_function
                params["existing_resource_id"] = auto_function
                params["target_resource_id"] = auto_function
                intent["resource_name"] = auto_function
                intent["parameters"] = params
                context.intent = intent
        if action == "update" and not function_name:
            return {
                "success": False,
                "requires_input": True,
                "question_prompt": "I need the Lambda function name to continue.",
                "questions": [
                    {
                        "variable": "existing_resource_id",
                        "question": "Provide the existing Lambda function name.",
                        "type": "string",
                    }
                ],
            }

        workflow_params = dict(params)
        workflow_params.setdefault("wait_for_active", True)
        result = await self.aws_executor.run_lambda_workflow(
            action=action,
            function_name=str(function_name) if function_name else None,
            parameters=workflow_params,
            tags=context.context_data.get("required_tags", {}),
            environment=context.environment,
        )
        result.setdefault("action", action)
        result.setdefault("resource_type", "lambda")
        if function_name:
            result.setdefault("function_name", str(function_name))
        return result

    async def _run_rds_service_workflow(self, context: WorkflowContext) -> Dict[str, Any]:
        intent = context.intent or {}
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        action = str(intent.get("action", "create")).strip().lower() or "create"
        db_identifier = (
            intent.get("resource_name")
            or params.get("existing_resource_id")
            or params.get("db_instance_id")
            or params.get("target_resource_id")
        )
        if action == "update" and not db_identifier:
            auto_db = await self._auto_select_existing_resource_name(
                resource_type="rds",
                request_text=context.natural_language_request,
                prefer_pending=True,
            )
            if auto_db:
                db_identifier = auto_db
                params["existing_resource_id"] = auto_db
                params["target_resource_id"] = auto_db
                intent["resource_name"] = auto_db
                intent["parameters"] = params
                context.intent = intent
        if action == "update" and not db_identifier:
            return {
                "success": False,
                "requires_input": True,
                "question_prompt": "I need the RDS instance identifier to continue.",
                "questions": [
                    {
                        "variable": "existing_resource_id",
                        "question": "Provide the existing RDS instance identifier.",
                        "type": "string",
                    }
                ],
            }

        workflow_params = dict(params)
        workflow_params.setdefault("wait_for_available", True)
        result = await self.aws_executor.run_rds_workflow(
            action=action,
            db_instance_id=str(db_identifier) if db_identifier else None,
            parameters=workflow_params,
            tags=context.context_data.get("required_tags", {}),
            environment=context.environment,
        )
        result.setdefault("action", action)
        result.setdefault("resource_type", "rds")
        if db_identifier:
            result.setdefault("db_instance_id", str(db_identifier))
        return result

    async def _run_generic_service_workflow(self, context: WorkflowContext) -> Dict[str, Any]:
        intent = context.intent or {}
        resource = str(intent.get("resource_type", "")).strip().lower()
        action = str(intent.get("action", "create")).strip().lower() or "create"
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        if action not in {"create", "update"}:
            return {"success": False, "error": f"Generic workflow supports create/update only (got {action})."}
        if not hasattr(self.aws_executor, f"_{action}_{resource}"):
            return {"success": False, "error": f"No static handler for {action}:{resource}."}

        resource_name = (
            intent.get("resource_name")
            or params.get("existing_resource_id")
            or params.get("target_resource_id")
        )
        if action == "update" and not resource_name:
            auto_target = await self._auto_select_existing_resource_name(
                resource_type=resource,
                request_text=context.natural_language_request,
                prefer_pending=True,
            )
            if auto_target:
                resource_name = auto_target
                params["existing_resource_id"] = auto_target
                params["target_resource_id"] = auto_target
                intent["resource_name"] = auto_target
                intent["parameters"] = params
                context.intent = intent
        if action == "update" and not resource_name:
            return {
                "success": False,
                "requires_input": True,
                "question_prompt": f"I need the target {resource} identifier/name to continue.",
                "questions": [
                    {
                        "variable": "existing_resource_id",
                        "question": f"Provide existing {resource} identifier/name.",
                        "type": "string",
                    }
                ],
            }

        result = await self.aws_executor.execute(
            action=action,
            resource_type=resource,
            resource_name=resource_name,
            parameters=params,
            tags=context.context_data.get("required_tags", {}),
        )
        if not isinstance(result, dict):
            return {"success": False, "error": "Generic workflow returned invalid response."}
        if not result.get("success"):
            return result

        result.setdefault("action", action)
        result.setdefault("resource_type", resource)
        identifier = self._extract_result_identifier(resource, result, intent, params)
        if identifier and not result.get("resource_name"):
            result["resource_name"] = identifier

        should_wait = bool(action in {"create", "update"} and self._to_bool(params.get("auto_wait_ready"), True))
        if should_wait and identifier and hasattr(self.aws_executor, f"_describe_{resource}"):
            wait = await self._wait_for_generic_resource_ready(
                resource_type=resource,
                identifier=str(identifier),
                timeout_seconds=int(params.get("auto_wait_timeout_seconds") or 1800),
                poll_seconds=int(params.get("auto_wait_poll_seconds") or 30),
            )
            result["generic_wait"] = wait
            if wait.get("success"):
                result["status"] = wait.get("state") or result.get("status")
            elif wait.get("pending"):
                result["readiness"] = "initializing"
                result["next_retry_seconds"] = int(wait.get("next_retry_seconds") or 30)

        return result

    async def _run_rds_sql_executor(self, context: WorkflowContext) -> Dict[str, Any]:
        intent = context.intent or {}
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        resource_name = intent.get("resource_name")

        sql_value = params.get("sql") or params.get("sql_statements")
        if isinstance(sql_value, list):
            sql_text = ";\n".join([str(item).strip().rstrip(";") for item in sql_value if str(item).strip()])
            if sql_text:
                sql_text = sql_text + ";"
        else:
            sql_text = str(sql_value or "").strip()

        if not sql_text:
            return {
                "success": False,
                "requires_input": True,
                "question_prompt": "Please provide SQL statements to run on the selected RDS instance.",
                "questions": [
                    {
                        "variable": "sql_statements",
                        "question": "Enter SQL statements (single line or multiple statements).",
                        "type": "string",
                        "hint": "Example: CREATE TABLE users(...); INSERT INTO users ...;",
                    }
                ],
            }

        target_db_instance = (
            params.get("existing_resource_id")
            or params.get("db_instance_id")
            or resource_name
        )
        if not target_db_instance:
            return {
                "success": False,
                "requires_input": True,
                "question_prompt": "I need the target RDS instance ID/name for SQL execution.",
                "questions": [
                    {"variable": "existing_resource_id", "question": "Provide RDS instance identifier.", "type": "string"}
                ],
            }

        ensure_network_path = params.get("ensure_network_path", True)
        if isinstance(ensure_network_path, str):
            normalized = ensure_network_path.strip().lower()
            if normalized in {"false", "0", "no", "n"}:
                ensure_network_path = False
            elif normalized in {"true", "1", "yes", "y"}:
                ensure_network_path = True
            else:
                ensure_network_path = True

        result = await self.aws_executor.execute_rds_sql_via_ssm(
            rds_instance_id=str(target_db_instance),
            sql=sql_text,
            db_name=params.get("db_name"),
            db_username=params.get("db_username"),
            db_password=params.get("db_password"),
            secret_arn=params.get("secret_arn"),
            bastion_instance_id=None if str(params.get("bastion_instance_id", "")).strip().lower() == "auto" else params.get("bastion_instance_id"),
            ensure_network_path=bool(ensure_network_path),
            wait_seconds=int(params.get("wait_seconds", 300) or 300),
        )
        if isinstance(result, dict):
            result.setdefault("action", "update")
            result.setdefault("resource_type", "rds")
        return result

    def _to_bool(self, value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().strip(" \t\r\n.;:!").lower()
            if lowered in {"true", "1", "yes", "y"}:
                return True
            if lowered in {"false", "0", "no", "n"}:
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    async def _auto_select_existing_resource_name(
        self,
        resource_type: str,
        request_text: str = "",
        prefer_pending: bool = False,
    ) -> Optional[str]:
        normalized_resource = str(resource_type or "").strip().lower()
        if not normalized_resource or not self.aws_executor.initialized:
            return None

        try:
            listed = await self.aws_executor.list_resource_choices(normalized_resource, limit=25)
        except Exception:
            return None
        if not isinstance(listed, dict) or not listed.get("success"):
            return None

        options_raw = listed.get("options", []) if isinstance(listed.get("options"), list) else []
        options: List[str] = []
        seen = set()
        for item in options_raw:
            candidate = str(item or "").strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            options.append(candidate)
        if not options:
            return None

        lowered_text = str(request_text or "").lower()
        for candidate in options:
            if candidate.lower() in lowered_text:
                return candidate
        if len(options) == 1:
            return options[0]

        intent_tokens = {"continue", "resume", "existing", "pending", "status", "update", "retry", "auto"}
        should_prioritize_pending = prefer_pending or any(token in lowered_text for token in intent_tokens)
        if not should_prioritize_pending:
            return options[0]

        if not hasattr(self.aws_executor, f"_describe_{normalized_resource}"):
            return options[0]

        for candidate in options[:10]:
            try:
                describe = await self.aws_executor.execute(
                    action="describe",
                    resource_type=normalized_resource,
                    resource_name=candidate,
                    parameters={},
                    tags={},
                )
            except Exception:
                continue
            if not isinstance(describe, dict) or not describe.get("success"):
                continue
            state_value = (
                describe.get("status")
                or describe.get("state")
                or describe.get("readiness")
                or describe.get("lifecycle_state")
                or describe.get("cluster_status")
                or describe.get("instance_status")
                or describe.get("DBInstanceStatus")
            )
            if self._classify_state(state_value) == "pending":
                return candidate

        return options[0]

    def _extract_result_identifier(
        self,
        resource_type: str,
        result: Dict[str, Any],
        intent: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Optional[str]:
        mapping = {
            "ec2": ["instance_id"],
            "eks": ["cluster_name", "resource_name"],
            "rds": ["db_instance_id"],
            "lambda": ["function_name"],
            "s3": ["bucket_name"],
            "vpc": ["vpc_id"],
            "subnet": ["subnet_id"],
            "security_group": ["group_id", "security_group_id"],
            "elb": ["load_balancer_name", "name"],
            "ecs": ["cluster_name", "service_name"],
            "emr": ["cluster_id", "cluster_name"],
            "redshift": ["cluster_id"],
            "elasticache": ["cluster_id"],
            "dynamodb": ["table_name"],
            "sagemaker": ["notebook_name", "name"],
            "codebuild": ["project_name"],
            "codepipeline": ["pipeline_name"],
            "glue": ["crawler_name", "database_name"],
            "apigateway": ["api_id", "api_name"],
            "cloudfront": ["distribution_id"],
            "wellarchitected": ["workload_id", "workload_name"],
        }
        keys = mapping.get(resource_type, [])
        for key in keys:
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for fallback in [intent.get("resource_name"), params.get("existing_resource_id"), params.get("target_resource_id")]:
            if isinstance(fallback, str) and fallback.strip():
                return fallback.strip()
        return None

    def _classify_state(self, value: Any) -> str:
        state = str(value or "").strip()
        normalized = state.lower().replace(" ", "")
        if not normalized:
            return "ready"
        pending_tokens = {
            "creating", "pending", "initializing", "starting", "provisioning",
            "inprogress", "in_progress", "updating", "modifying", "configuring",
        }
        fail_tokens = {
            "failed", "error", "deleted", "deleting", "terminate", "terminated",
            "incompatible", "rollback", "cancelled", "canceled",
        }
        ready_tokens = {
            "active", "available", "running", "ready", "ok", "completed", "enabled", "issued", "inservice",
        }
        if any(token in normalized for token in fail_tokens):
            return "failed"
        if any(token in normalized for token in pending_tokens):
            return "pending"
        if any(token in normalized for token in ready_tokens):
            return "ready"
        return "ready"

    async def _wait_for_generic_resource_ready(
        self,
        resource_type: str,
        identifier: str,
        timeout_seconds: int = 1800,
        poll_seconds: int = 30,
    ) -> Dict[str, Any]:
        waited = 0
        max_wait = max(int(timeout_seconds or 0), 0)
        delay = max(int(poll_seconds or 0), 5)
        state_keys = ["status", "state", "lifecycle_state", "cluster_status", "instance_status", "DBInstanceStatus"]
        while waited <= max_wait:
            described = await self.aws_executor.execute(
                action="describe",
                resource_type=resource_type,
                resource_name=identifier,
                parameters={},
                tags={},
            )
            if not described.get("success"):
                text = str(described.get("error") or "")
                lowered = text.lower()
                if any(token in lowered for token in ["not found", "does not exist", "no such"]):
                    return {"success": False, "failed": True, "error": text, "state": "not_found"}
                return {"success": False, "pending": True, "state": "unknown", "error": text, "next_retry_seconds": delay}

            state_value = None
            for key in state_keys:
                value = described.get(key)
                if isinstance(value, str) and value.strip():
                    state_value = value
                    break
            classification = self._classify_state(state_value)
            if classification == "ready":
                return {"success": True, "ready": True, "state": state_value or "ready", "describe": described}
            if classification == "failed":
                return {"success": False, "failed": True, "state": state_value or "failed", "describe": described}

            await asyncio.sleep(delay)
            waited += delay

        return {"success": False, "pending": True, "state": "pending", "next_retry_seconds": delay}

    def _apply_input_variables(self, context: WorkflowContext, input_variables: Dict[str, Any]) -> WorkflowContext:
        if not input_variables:
            return context
        if not context.intent:
            context.intent = {}
        context.intent.setdefault("parameters", {})

        for key, value in input_variables.items():
            if key in {"region", "aws_region"}:
                context.intent["region"] = value
            elif key in {"resource_name", "name"}:
                context.intent["resource_name"] = value
            elif key in {"aws_access_key", "aws_secret_key"}:
                # Credentials are consumed at request level, not passed as intent parameters.
                continue
            else:
                context.intent["parameters"][key] = value

        return context

    def _apply_resource_selection_to_intent(self, context: WorkflowContext) -> WorkflowContext:
        intent = context.intent or {}
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        requested_action = str(intent.get("action", "")).strip().lower()
        current_resource = str(intent.get("resource_type", "")).strip().lower()

        alignment_choice = str(params.get("service_alignment_decision", "")).strip().lower()
        if alignment_choice.startswith("switch_"):
            target_resource = alignment_choice.replace("switch_", "", 1).strip()
            if target_resource and target_resource != current_resource:
                intent["resource_type"] = target_resource
                if str(params.get("resource_strategy", "")).strip().lower() == "existing":
                    intent["action"] = "update"
                    params["existing_operation"] = "custom"
                    params["custom_operation"] = "custom"
                    params.pop("existing_resource_id", None)
                    params.pop("target_resource_id", None)
                    intent["resource_name"] = None
                current_resource = target_resource

        manual_switch_choice = str(params.get("switch_resource_type", "")).strip().lower()
        if manual_switch_choice.startswith("switch_"):
            target_resource = manual_switch_choice.replace("switch_", "", 1).strip()
            if target_resource and target_resource != current_resource:
                intent["resource_type"] = target_resource
                params.pop("existing_resource_id", None)
                params.pop("target_resource_id", None)
                params.pop("existing_operation", None)
                params.pop("custom_instruction", None)
                intent["resource_name"] = None
                current_resource = target_resource

        selected_existing_raw = str(params.get("existing_resource_id", "")).strip().lower()
        if selected_existing_raw == "use_new_default":
            params["resource_strategy"] = "new"
            params.pop("existing_resource_id", None)
            params.pop("target_resource_id", None)
            params.pop("existing_operation", None)
            params.pop("custom_instruction", None)
            if requested_action:
                intent["action"] = requested_action
            intent["resource_name"] = None

        strategy = str(params.get("resource_strategy", "")).strip().lower()
        selected = params.get("existing_resource_id") or params.get("resource_name")
        selected_text = str(selected).strip() if selected is not None else ""
        selected_value = selected_text.split("|")[0].strip() if "|" in selected_text else selected_text

        if selected_value:
            # Reuse selected existing resource as target identifier/name.
            intent["resource_name"] = selected_value
            params["target_resource_id"] = selected_value

        operation = str(params.get("existing_operation", "")).strip().lower()
        if strategy == "existing":
            if requested_action == "create" and operation in {"create", "update"}:
                # "Create using existing resource" should be handled as custom/update intent.
                operation = "custom"
                params["existing_operation"] = "custom"
                params["custom_operation"] = "custom"
            if operation in {"update", "delete", "describe", "list", "create"}:
                intent["action"] = operation
            elif operation in {"custom", "create_child"}:
                # Treat custom instructions on existing resources as update-style operations.
                intent["action"] = "update"
                params["custom_operation"] = operation
                if str(intent.get("resource_type", "")).lower().strip() == "rds":
                    if params.get("sql") or params.get("sql_statements"):
                        params["run_sql_via_ssm"] = True

        intent["parameters"] = params
        context.intent = intent
        return context

    def _collect_service_alignment_questions(
        self,
        intent: Dict[str, Any],
        request_text: str,
    ) -> List[Dict[str, Any]]:
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        current_resource = str(intent.get("resource_type", "")).strip().lower()
        if not current_resource:
            return []

        operation = str(params.get("existing_operation", "")).strip().lower()
        custom_instruction = str(params.get("custom_instruction", "")).strip()
        analysis_text = custom_instruction if operation == "custom" and custom_instruction else str(request_text or "")
        if not analysis_text:
            return []

        inferred_resources = self._infer_services_from_text(analysis_text)
        if not inferred_resources:
            return []
        inferred_resource = inferred_resources[0]
        if inferred_resource == current_resource:
            return []
        if current_resource in inferred_resources[:2]:
            return []

        alignment_choice = str(params.get("service_alignment_decision", "")).strip().lower()
        continue_choice = f"continue_{current_resource}"
        switch_choices = [f"switch_{item}" for item in inferred_resources[:3] if item != current_resource]
        if not switch_choices:
            return []
        if alignment_choice in {continue_choice, *switch_choices}:
            return []

        reason = self._alignment_reason_phrase(analysis_text, inferred_resource)
        options = [continue_choice] + switch_choices
        return [
            {
                "variable": "service_alignment_decision",
                "question": (
                    f"This instruction looks like a {inferred_resource.upper()} task ({reason}). "
                    f"Do you want to continue with {current_resource.upper()} or switch to the best-matched service?"
                ),
                "type": "string",
                "options": options,
                "hint": "Switch to auto-route and continue with correct service prerequisites.",
            }
        ]

    def _infer_services_from_text(self, text: str) -> List[str]:
        lowered = str(text or "").lower()
        if not lowered.strip():
            return []

        scores: Dict[str, int] = {}
        for resource, patterns in self.nlu_service.resource_patterns.items():
            score = 0
            for token in patterns:
                tok = str(token or "").strip().lower()
                if not tok:
                    continue
                if " " in tok:
                    if tok in lowered:
                        score += 3
                elif re.search(rf"\b{re.escape(tok)}\b", lowered):
                    score += 2
            if score > 0:
                scores[resource] = score

        # Strong boosts for high-intent phrases.
        if "fargate" in lowered:
            scores["ecs"] = scores.get("ecs", 0) + 9
        if "api gateway" in lowered or "http api" in lowered or "rest api" in lowered:
            scores["apigateway"] = scores.get("apigateway", 0) + 9
        if "glue" in lowered or "etl" in lowered or "crawler" in lowered:
            scores["glue"] = scores.get("glue", 0) + 9
        if "waf" in lowered or "firewall" in lowered:
            scores["waf"] = scores.get("waf", 0) + 9
            scores["security_group"] = scores.get("security_group", 0) + 5
        if "kubernetes" in lowered or "k8s" in lowered or "eks" in lowered:
            scores["eks"] = scores.get("eks", 0) + 9
        if "serverless" in lowered or "server less" in lowered or "server-less" in lowered:
            scores["lambda"] = scores.get("lambda", 0) + 8
            scores["apigateway"] = scores.get("apigateway", 0) + 5
        if "3 tier" in lowered or "three tier" in lowered:
            scores["vpc"] = scores.get("vpc", 0) + 8
            scores["elb"] = scores.get("elb", 0) + 7
            scores["rds"] = scores.get("rds", 0) + 7

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        return [resource for resource, score in ranked if score >= 3][:5]

    def _infer_service_from_text(self, text: str) -> Optional[str]:
        ranked = self._infer_services_from_text(text)
        if ranked:
            return ranked[0]
        lowered = str(text or "").lower()
        if not lowered.strip():
            return None

        if re.search(r"\b(create|alter|drop|insert|update|delete)\s+table\b", lowered) or "sql" in lowered:
            return "rds"
        if "dynamodb" in lowered or re.search(r"\bnosql\b", lowered):
            return "dynamodb"
        if "kubernetes" in lowered or "eks" in lowered:
            return "eks"
        if "bucket" in lowered or "s3" in lowered:
            return "s3"
        if "lambda" in lowered or "serverless" in lowered or "server less" in lowered or "server-less" in lowered:
            return "lambda"
        if "api gateway" in lowered or "http api" in lowered or "rest api" in lowered:
            return "apigateway"
        if "vpc" in lowered or "subnet" in lowered or "route table" in lowered:
            return "vpc"
        if "security group" in lowered or "ingress" in lowered or "egress" in lowered:
            return "security_group"
        if "cluster" in lowered and "ecs" in lowered:
            return "ecs"
        if "rds" in lowered or "postgres" in lowered or "mysql" in lowered or "database" in lowered:
            return "rds"
        if "ec2" in lowered or "instance" in lowered or "server" in lowered:
            return "ec2"

        scores: Dict[str, int] = {}
        for resource, patterns in self.nlu_service.resource_patterns.items():
            score = 0
            for token in patterns:
                tok = str(token or "").strip().lower()
                if not tok:
                    continue
                if " " in tok:
                    if tok in lowered:
                        score += 2
                elif re.search(rf"\b{re.escape(tok)}\b", lowered):
                    score += 1
            if score > 0:
                scores[resource] = score
        if not scores:
            return None

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        best_resource, best_score = ranked[0]
        if best_score < 2:
            return None
        return best_resource

    def _alignment_reason_phrase(self, text: str, inferred: str) -> str:
        lowered = str(text or "").lower()
        if inferred == "rds" and ("sql" in lowered or "table" in lowered):
            return "SQL/table keywords detected"
        if inferred == "s3":
            return "bucket/object storage keywords detected"
        if inferred == "lambda":
            return "serverless/function keywords detected"
        if inferred == "ecs":
            return "Fargate/ECS container keywords detected"
        if inferred == "apigateway":
            return "API Gateway keywords detected"
        if inferred == "glue":
            return "ETL/Glue keywords detected"
        if inferred == "waf":
            return "firewall/WAF keywords detected"
        if inferred == "eks":
            return "Kubernetes/EKS keywords detected"
        if inferred == "vpc":
            return "networking/VPC keywords detected"
        return f"{inferred.upper()} keywords detected"

    def _attach_clarification_questions(self, context: WorkflowContext) -> WorkflowContext:
        result = context.execution_result or {}
        if result.get("success"):
            return context
        if result.get("requires_input"):
            return context

        errors = self._collect_errors(result)
        questions = self._build_questions_from_errors(errors, context.intent or {})
        if questions:
            result["requires_input"] = True
            result["questions"] = questions
            result["question_prompt"] = "I need a few values before I can continue."
            context.execution_result = result
        return context

    def _collect_errors(self, payload: Any) -> List[str]:
        errors = []
        if isinstance(payload, dict):
            if isinstance(payload.get("error"), str) and payload["error"].strip():
                errors.append(payload["error"].strip())
            for value in payload.values():
                errors.extend(self._collect_errors(value))
        elif isinstance(payload, list):
            for item in payload:
                errors.extend(self._collect_errors(item))
        return errors

    def _build_questions_from_errors(self, errors: List[str], intent: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not errors:
            return []

        questions: List[Dict[str, Any]] = []
        joined = " | ".join(errors).lower()
        role_fields = {"role", "rolearn", "executionrolearn", "servicerole"}

        def add_question(variable: str, question: str, qtype: str = "string", hint: Optional[str] = None):
            if any(q.get("variable") == variable for q in questions):
                return
            item = {"variable": variable, "question": question, "type": qtype}
            if hint:
                item["hint"] = hint
            questions.append(item)

        if "unable to locate credentials" in joined or "credentials not provided" in joined:
            add_question("aws_access_key", "Please provide your AWS Access Key ID.", hint="Starts with AKIA...")
            add_question("aws_secret_key", "Please provide your AWS Secret Access Key.", qtype="password")

        if "you must specify a region" in joined or "noregionerror" in joined:
            add_question("aws_region", "Which AWS region should I use?", hint="Example: us-east-1")

        if "invalidkeypair.notfound" in joined or "key pair" in joined:
            add_question("key_name", "Which EC2 key pair should I attach?", hint="Existing key pair name in this region")

        if "invalidamiid.notfound" in joined:
            add_question("ami_id", "Please provide a valid AMI ID for this region.", hint="Example: ami-xxxxxxxxxxxxxxxxx")

        if "missing required parameter in input" in joined:
            for err in errors:
                parts = err.split("Missing required parameter in input:")
                if len(parts) > 1:
                    param = parts[1].strip().strip("'\" ")
                    if param:
                        normalized = re.sub(r"[^a-zA-Z0-9]", "", str(param or "")).lower()
                        if normalized in role_fields:
                            add_question(
                                "iam_permissions_ready",
                                "I can auto-create required IAM roles, but this run is missing IAM permissions. Please allow IAM role-management permissions and retry.",
                                hint="Required permissions include iam:CreateRole, iam:AttachRolePolicy, iam:PassRole.",
                            )
                            continue
                        add_question(param, f"Please provide value for '{param}'.")

        if ("accessdenied" in joined or "not authorized" in joined) and ("iam" in joined or "role" in joined):
            add_question(
                "iam_permissions_ready",
                "I could not auto-create required IAM roles due account permissions. Please grant IAM role-management permissions and retry.",
                hint="Required permissions include iam:CreateRole, iam:AttachRolePolicy, iam:PassRole.",
            )

        if "freetierrestrictionerror" in joined or "free plan" in joined:
            add_question(
                "instance_type",
                "Your account plan blocked the selected DB instance class. Choose a smaller/allowed class.",
                hint="Try: db.t4g.micro or db.t3.micro",
            )

        if "masterusername" in joined and "reserved word" in joined:
            add_question(
                "master_username",
                "The DB master username is reserved. Provide a different username.",
                hint="Example: dbadmin",
            )

        if "no module named 'psycopg2'" in joined:
            add_question(
                "execution_mode",
                "Dynamic code used unsupported libraries. Continue with static boto-only execution?",
                hint="Type: static",
            )

        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        custom_network = self._to_bool(
            params.get("use_custom_networking", params.get("custom_networking")),
            default=False,
        )
        if (
            not questions
            and intent.get("action") == "create"
            and intent.get("resource_type") == "ec2"
            and custom_network
        ):
            # Ask network specifics only when user explicitly requested custom networking.
            add_question("subnet_id", "Provide the subnet ID to use for this EC2 instance.", hint="subnet-xxxxxxxx")

        return questions

    def _default_phase_plan(self) -> List[Dict[str, Any]]:
        return [
            {"id": "design_plan", "title": "Design + plan", "status": "pending"},
            {"id": "networking_security", "title": "Provision networking/security", "status": "pending"},
            {"id": "compute_data", "title": "Provision compute/data", "status": "pending"},
            {"id": "deploy_app", "title": "Deploy app", "status": "pending"},
            {"id": "validate_health", "title": "Validate health checks", "status": "pending"},
        ]

    def _initialize_phases(self, context: WorkflowContext):
        context.context_data["phases"] = self._default_phase_plan()

    def _update_phase(self, context: WorkflowContext, phase_id: str, status: str, detail: Optional[str] = None):
        phases = context.context_data.get("phases", [])
        for phase in phases:
            if phase.get("id") == phase_id:
                phase["status"] = status
                if detail:
                    phase["detail"] = detail
                phase["updated_at"] = datetime.utcnow().isoformat()
                break

    def _skip_remaining_phases(self, context: WorkflowContext, start_phase_id: str, reason: str):
        phases = context.context_data.get("phases", [])
        skipping = False
        for phase in phases:
            if phase.get("id") == start_phase_id:
                skipping = True
            if skipping and phase.get("status") in {"pending", "in_progress"}:
                phase["status"] = "skipped"
                phase["detail"] = reason
                phase["updated_at"] = datetime.utcnow().isoformat()

    def _finalize_phases_for_success(self, context: WorkflowContext, result: Dict[str, Any]):
        intent = context.intent or {}
        action = str(result.get("action") or intent.get("action") or "").lower().strip()
        parameters = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        resource_type = str(result.get("resource_type") or intent.get("resource_type") or "").lower().strip()
        install_requested = bool(parameters.get("install_targets") or parameters.get("user_data"))
        readiness = str(result.get("readiness", "")).lower()
        outcome_validation = result.get("outcome_validation", {}) if isinstance(result.get("outcome_validation"), dict) else {}
        summary = outcome_validation.get("summary", {}) if isinstance(outcome_validation.get("summary"), dict) else {}
        failed_count = int(summary.get("failed", 0) or 0)
        pending_count = int(summary.get("pending", 0) or 0)
        checks_total = int(summary.get("total", 0) or 0)
        phase_hints = outcome_validation.get("phase_hints", {}) if isinstance(outcome_validation.get("phase_hints"), dict) else {}

        self._update_phase(context, "networking_security", "completed", "Networking and security provisioning completed.")
        self._update_phase(context, "compute_data", "completed", "Compute/data resources created successfully.")

        deploy_completed = False
        deploy_detail = "No application deployment requested."
        if resource_type == "ec2":
            installation_mode = str(result.get("installation_mode", "")).strip().lower()
            deploy_completed = install_requested or (installation_mode not in {"", "none"})
            if deploy_completed:
                deploy_detail = "Application bootstrap script submitted."
        elif resource_type == "s3" and result.get("website_configuration") and result.get("website_url"):
            deploy_completed = True
            deploy_detail = "Static website endpoint configured and content deployed."
        elif resource_type == "s3" and (
            parameters.get("website_configuration")
            or parameters.get("website_enabled")
            or result.get("website_configuration")
        ):
            deploy_completed = True
            deploy_detail = "Static website configuration applied."
        elif any(result.get(key) for key in ("website_url", "invoke_url", "endpoint", "api_url", "app_url", "public_url", "ingress_url")):
            deploy_completed = True
            deploy_detail = "Application endpoint is available."
        elif action == "update" and any(parameters.get(k) for k in ("install_targets", "app_targets", "custom_commands", "sql", "sql_statements")):
            deploy_completed = True
            deploy_detail = "Application/data updates applied."
        if isinstance(phase_hints.get("deploy_completed"), bool):
            deploy_completed = bool(phase_hints.get("deploy_completed"))
            deploy_detail = str(phase_hints.get("deploy_detail") or deploy_detail)

        if deploy_completed:
            self._update_phase(context, "deploy_app", "completed", deploy_detail)
        else:
            self._update_phase(context, "deploy_app", "skipped", deploy_detail)

        if outcome_validation.get("performed"):
            if failed_count > 0:
                self._update_phase(
                    context,
                    "validate_health",
                    "completed",
                    str(phase_hints.get("health_detail") or f"Validation completed with warnings ({failed_count}/{checks_total or failed_count} checks failed)."),
                )
            elif pending_count > 0:
                self._update_phase(
                    context,
                    "validate_health",
                    "in_progress",
                    str(phase_hints.get("health_detail") or f"Validation is still in progress ({pending_count} checks pending)."),
                )
            else:
                self._update_phase(
                    context,
                    "validate_health",
                    "completed",
                    str(phase_hints.get("health_detail") or f"Validation completed ({checks_total} checks passed)."),
                )
            return

        if resource_type and resource_type != "ec2":
            self._update_phase(context, "validate_health", "completed", "Validation completed.")
            return

        if readiness.startswith("instance_status_ok"):
            self._update_phase(context, "validate_health", "completed", "Instance status checks passed.")
        elif any(token in readiness for token in ("pending", "launch_submitted", "initializing", "starting")) or not readiness:
            self._update_phase(context, "validate_health", "in_progress", "Health validation is still in progress.")
        else:
            self._update_phase(context, "validate_health", "completed", "Validation completed.")

    def _finalize_phases_for_failure(self, context: WorkflowContext, error: Optional[str]):
        self._update_phase(context, "networking_security", "failed", error or "Execution failed.")
        self._skip_remaining_phases(context, "compute_data", "Skipped after failure.")

    def _fail_active_phase(self, context: WorkflowContext, error: str):
        phases = context.context_data.get("phases", [])
        for phase in phases:
            if phase.get("status") == "in_progress":
                phase["status"] = "failed"
                phase["detail"] = error
                phase["updated_at"] = datetime.utcnow().isoformat()
                return
        if phases:
            phases[0]["status"] = "failed"
            phases[0]["detail"] = error
            phases[0]["updated_at"] = datetime.utcnow().isoformat()

    def _attach_phases_to_result(self, context: WorkflowContext):
        if context.execution_result is None:
            context.execution_result = {"success": False, "error": "No execution result available."}
        if isinstance(context.execution_result, dict):
            context.execution_result["phases"] = context.context_data.get("phases", [])

    def _is_complex_request(self, prompt: str) -> bool:
        text = (prompt or "").lower()
        keys = [
            "3 tier",
            "three tier",
            "multi tier",
            "eks",
            "k8s",
            "kubernetes",
            "kubernates",
            "kubernets",
            "kubernete",
            "spark",
            "private cluster",
            "microservices",
            "alb",
            "rds",
            "serverless",
            "server less",
            "server-less",
            "fargate",
            "ecs",
            "api gateway",
            "glue",
            "etl",
            "waf",
            "firewall",
            "athena",
            "data pipeline",
        ]
        return any(k in text for k in keys)

    def _is_kubernetes_website_request(self, original: str, intent: ParsedIntent) -> bool:
        text = (original or "").lower()
        params = intent.parameters or {}
        resource = str(intent.resource_type or "").lower().strip()
        kubernetes_signal = any(
            token in text
            for token in ["kubernetes", "k8s", "eks", "kubernates", "kubernets", "kubernete"]
        ) or resource == "eks"
        website_signal = any(token in text for token in ["website", "web app", "webapp", "frontend", "sample site"])
        public_url_signal = any(token in text for token in ["public", "url", "internet", "accessible"])
        return kubernetes_signal and (website_signal or params.get("website_accessibility")) and public_url_signal

    def _compose_improved_prompt(self, original: str, intent: ParsedIntent, environment: str, region: Optional[str]) -> str:
        target_region = region or intent.region or "us-east-1"
        params = intent.parameters or {}
        service_targets = params.get("service_targets") if isinstance(params.get("service_targets"), list) else []
        service_targets = [str(item).strip().lower() for item in service_targets if str(item).strip()]
        if not service_targets:
            inferred = self._infer_services_from_text(original or "")
            service_targets = inferred[:5]

        resource_type = str(intent.resource_type or "").strip().lower()
        if (not resource_type or resource_type == "unknown") and service_targets:
            resource_type = service_targets[0]

        resource = resource_type.upper() if resource_type else "AWS resource"
        action = intent.action.upper() if intent.action else "CREATE"
        name_part = f" named {intent.resource_name}" if intent.resource_name else ""
        param_chunks = [f"{k}={v}" for k, v in params.items() if isinstance(v, (str, int, float, bool))]
        param_line = f" Parameters: {', '.join(param_chunks)}." if param_chunks else ""
        text_lower = (original or "").lower()
        condensed = "".join(ch for ch in text_lower if ch.isalnum())
        is_serverless = (
            "serverless" in condensed
            or "server less" in text_lower
            or "server-less" in text_lower
            or params.get("architecture_style") == "serverless"
        )
        architecture_style = str(params.get("architecture_style") or "").strip().lower()
        target_line = ""
        if service_targets:
            target_line = f"Target services: {', '.join(s.upper() for s in service_targets)}.\n"

        if is_serverless or architecture_style in {"serverless", "serverless_web"}:
            return (
                f"Goal: Build a serverless web application in {target_region} for {environment}.\n"
                f"{target_line}"
                "Execution Plan:\n"
                "1) Design + plan (confirm API type, frontend hosting, auth, and data store)\n"
                "2) Provision networking/security (IAM roles, least-privilege policies, optional WAF)\n"
                "3) Provision compute/data (Lambda + API Gateway + optional DynamoDB/S3)\n"
                "4) Deploy app/workloads\n"
                "5) Validate health checks and endpoint tests\n"
                "Ask follow-up questions for missing values before each phase, then execute phase-by-phase."
            )

        if self._is_kubernetes_website_request(original, intent):
            return (
                f"Goal: Deploy a sample public website on Kubernetes (EKS) in {target_region} for {environment}.\n"
                f"{target_line}"
                "Execution Plan:\n"
                "1) Design + plan (cluster mode, node sizing, domain/TLS optional)\n"
                "2) Provision networking/security (VPC/subnets/SG/IAM + EKS prerequisites)\n"
                "3) Provision compute/data (EKS cluster + node groups)\n"
                "4) Deploy app/workloads (namespace, deployment, service, ingress/load balancer)\n"
                "5) Validate health checks (kubectl status, endpoint checks) and return final public URL\n"
                "Ask follow-up questions for missing values before each phase, then execute phase-by-phase."
            )

        if architecture_style == "container_fargate" or ("fargate" in text_lower or "ecs" in service_targets):
            return (
                f"Goal: Build and deploy a containerized application on ECS Fargate in {target_region} for {environment}.\n"
                f"{target_line}"
                "Execution Plan:\n"
                "1) Design + plan (service type, CPU/memory, image source, scaling)\n"
                "2) Provision networking/security (VPC/subnets/SG/ALB/IAM task roles)\n"
                "3) Provision compute/data (ECS cluster, task definition, service)\n"
                "4) Deploy app/workloads (container image and rollout)\n"
                "5) Validate health checks (target group, service events, endpoint tests)\n"
                "Ask follow-up questions for missing values before each phase, then execute phase-by-phase."
            )

        if architecture_style == "data_pipeline" or ("glue" in service_targets):
            return (
                f"Goal: Build a data pipeline in {target_region} for {environment}.\n"
                f"{target_line}"
                "Execution Plan:\n"
                "1) Design + plan (source, transform, schedule, target)\n"
                "2) Provision networking/security (IAM roles, data access policies, encryption)\n"
                "3) Provision compute/data (Glue jobs/crawlers/catalog, optional Athena/S3)\n"
                "4) Deploy app/workloads (job scripts/workflows/triggers)\n"
                "5) Validate health checks (job runs, logs, sample query checks)\n"
                "Ask follow-up questions for missing values before each phase, then execute phase-by-phase."
            )

        if architecture_style == "security_hardening" or any(s in service_targets for s in {"security_group", "waf", "iam"}):
            return (
                f"Goal: Apply security controls in {target_region} for {environment}.\n"
                f"{target_line}"
                "Execution Plan:\n"
                "1) Design + plan (scope, policies, inbound/outbound rules, protection level)\n"
                "2) Provision networking/security (SG/WAF/IAM changes with approvals)\n"
                "3) Provision compute/data (apply required attachments/associations)\n"
                "4) Deploy app/workloads (if workload updates are required)\n"
                "5) Validate health checks (policy/rule verification and connectivity tests)\n"
                "Ask follow-up questions for missing values before each phase, then execute phase-by-phase."
            )

        if self._is_complex_request(original) or len(service_targets) > 1:
            goal_line = original.strip()
            if not goal_line:
                goal_line = f"{action} {resource}"
            return (
                f"Goal: {goal_line}\n"
                f"Environment: {environment}\n"
                f"Region: {target_region}\n"
                f"{target_line}"
                "Execution Plan:\n"
                "1) Design + plan\n"
                "2) Provision networking/security\n"
                "3) Provision compute/data\n"
                "4) Deploy app/workloads\n"
                "5) Validate health checks\n"
                "Ask follow-up questions for missing values before each phase, then execute phase-by-phase."
            )

        return (
            f"{action} {resource}{name_part} in {target_region} for {environment} environment."
            f"{param_line} Ask follow-up questions for missing values before execution."
        )

    async def _collect_prerequisite_questions(
        self,
        intent: Dict[str, Any],
        request_data: Optional[Dict[str, Any]] = None,
        input_variables: Optional[Dict[str, Any]] = None,
        request_text: str = "",
    ) -> List[Dict[str, Any]]:
        intent = intent or {}
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        if self._request_mentions_custom_networking(request_text):
            params["use_custom_networking"] = True
        intent["parameters"] = params
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        input_variables = input_variables or {}
        request_data = request_data or {}
        intent = await self._auto_fill_prerequisites_from_aws(
            intent=intent,
            request_data=request_data,
            input_variables=input_variables,
        )

        selector_questions = await self._collect_existing_resource_selection_questions(
            intent=intent,
            request_data=request_data,
            input_variables=input_variables,
        )
        if selector_questions:
            return selector_questions

        alignment_questions = self._collect_service_alignment_questions(
            intent=intent,
            request_text=request_text,
        )
        if alignment_questions:
            return alignment_questions

        dynamic_questions = self.prerequisite_schema.build_questions(intent)
        fallback_questions = self._collect_legacy_prerequisite_questions(intent or {})
        merged: List[Dict[str, Any]] = []
        seen = set()
        for item in dynamic_questions + fallback_questions:
            var = item.get("variable")
            if not var or var in seen:
                continue
            seen.add(var)
            merged.append(item)
        return merged

    def _request_mentions_custom_networking(self, request_text: str) -> bool:
        lowered = str(request_text or "").lower()
        if not lowered.strip():
            return False
        explicit_patterns = [
            r"\bcustom\s+(subnet|subnets|vpc|security group|security-group|network|firewall)\b",
            r"\bspecific\s+(subnet|vpc|security group|security-group)\b",
            r"\bmy\s+(subnet|subnets|vpc|security group|security-group|firewall)\b",
            r"\buse\s+(subnet-|sg-|vpc-)",
            r"\bprovide\s+(subnet|vpc|security group|firewall)\b",
            r"\bopen\s+port\b.*\bmanually\b",
            r"\bmanual\s+(network|networking|firewall|security group|security-group)\b",
        ]
        return any(re.search(pattern, lowered) for pattern in explicit_patterns)

    async def _auto_fill_prerequisites_from_aws(
        self,
        intent: Dict[str, Any],
        request_data: Dict[str, Any],
        input_variables: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(intent, dict):
            return intent

        action = str(intent.get("action", "")).strip().lower()
        resource = str(intent.get("resource_type", "")).strip().lower()
        if action not in {"create", "update"} or not resource:
            return intent

        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        aws_access_key = (
            request_data.get("aws_access_key")
            or input_variables.get("aws_access_key")
            or params.get("aws_access_key")
        )
        aws_secret_key = (
            request_data.get("aws_secret_key")
            or input_variables.get("aws_secret_key")
            or params.get("aws_secret_key")
        )
        aws_region = (
            intent.get("region")
            or request_data.get("aws_region")
            or input_variables.get("aws_region")
            or "us-east-1"
        )
        if not (aws_access_key and aws_secret_key):
            return intent

        try:
            self.aws_executor.initialize(aws_access_key, aws_secret_key, aws_region)
            auto = await self.aws_executor.auto_fill_intent_prerequisites(
                action=action,
                resource_type=resource,
                resource_name=intent.get("resource_name"),
                parameters=params,
                tags={
                    "ManagedBy": "AI-Platform",
                    "Environment": str(request_data.get("environment") or "dev"),
                },
                environment=str(request_data.get("environment") or "dev"),
            )
            if auto.get("success") and isinstance(auto.get("parameters"), dict):
                intent["parameters"] = auto.get("parameters")
        except Exception:
            return intent

        return intent

    async def _collect_existing_resource_selection_questions(
        self,
        intent: Dict[str, Any],
        request_data: Dict[str, Any],
        input_variables: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        action = str(intent.get("action", "")).lower().strip()
        resource = str(intent.get("resource_type", "")).lower().strip()
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        questions: List[Dict[str, Any]] = []

        mutable_actions = {"create", "update", "delete"}
        if action not in mutable_actions:
            return questions

        def add_q(variable: str, question: str, qtype: str = "string", hint: Optional[str] = None, options: Optional[List[Any]] = None):
            if params.get(variable) not in (None, "", []):
                return
            item: Dict[str, Any] = {"variable": variable, "question": question, "type": qtype}
            if hint:
                item["hint"] = hint
            if options:
                item["options"] = options
            questions.append(item)

        strategy = str(params.get("resource_strategy", "")).strip().lower()
        if action == "create" and strategy not in {"new", "existing"}:
            # Fast default path: create new unless user explicitly asks for existing.
            params["resource_strategy"] = "new"
            intent["parameters"] = params
            strategy = "new"

        if action in {"update", "delete"} and strategy != "existing":
            params["resource_strategy"] = "existing"
            strategy = "existing"

        if strategy != "existing":
            return questions

        if params.get("existing_resource_id") in (None, "", []):
            resource_name = str(intent.get("resource_name") or "").strip()
            if resource_name and not self._existing_resource_mismatch_reason(resource, resource_name):
                params["existing_resource_id"] = resource_name

        # Existing resource flow requires AWS auth to fetch inventory.
        aws_access_key = request_data.get("aws_access_key") or input_variables.get("aws_access_key")
        aws_secret_key = request_data.get("aws_secret_key") or input_variables.get("aws_secret_key")
        aws_region = (
            intent.get("region")
            or request_data.get("aws_region")
            or input_variables.get("aws_region")
            or "us-east-1"
        )
        if not (aws_access_key and aws_secret_key):
            add_q("aws_access_key", "Provide AWS Access Key to list existing resources.", hint="Starts with AKIA...")
            add_q("aws_secret_key", "Provide AWS Secret Access Key.", qtype="password")
            add_q("aws_region", "Which region should I inspect for existing resources?", hint="Example: us-east-1")
            return questions

        self.aws_executor.initialize(aws_access_key, aws_secret_key, aws_region)
        list_result = await self.aws_executor.list_resource_choices(resource)
        options = list_result.get("options", []) if isinstance(list_result, dict) else []

        if params.get("existing_resource_id") in (None, "", []):
            if not options:
                # Chat-flow discovery fallback for the selected service only.
                discovery = await self.aws_executor.discover_account_resources(
                    resource_types=[resource],
                    per_type_limit=25,
                    include_empty=True,
                )
                inventory = discovery.get("inventory", {}) if isinstance(discovery, dict) else {}
                sample_ids = (inventory.get(resource, {}) or {}).get("sample_ids", [])
                if isinstance(sample_ids, list):
                    options = [str(item).strip() for item in sample_ids if str(item).strip()]
            if options:
                option_values = options[:25]
                if action == "create":
                    option_values = ["use_new_default"] + option_values
                add_q(
                    "existing_resource_id",
                    f"Select existing {resource} to use.",
                    options=option_values,
                )
                if action == "create":
                    questions[-1]["hint"] = "Choose use_new_default to proceed with new resource creation."
            else:
                add_q(
                    "existing_resource_id",
                    f"No existing {resource} resources were found. Enter resource ID/name manually or type use_new_default.",
                    hint=self._expected_identifier_hint(resource),
                )
            return questions

        selected_existing = str(params.get("existing_resource_id") or "").strip()
        mismatch_reason = self._existing_resource_mismatch_reason(resource, selected_existing)
        if mismatch_reason:
            add_q(
                "existing_resource_id",
                mismatch_reason,
                hint=self._expected_identifier_hint(resource),
            )
            return questions

        if params.get("existing_operation") in (None, "", []):
            op_options = ["update", "delete", "describe", "custom"]
            if action == "create":
                op_options = ["custom", "describe", "update"]
            add_q(
                "existing_operation",
                f"What should I do with the selected {resource}?",
                options=op_options,
            )
            return questions

        op = str(params.get("existing_operation", "")).strip().lower()
        if action == "create" and strategy == "existing" and op in {"create", "update", "custom", "create_child"} and params.get("custom_instruction") in (None, "", []):
            add_q(
                "custom_instruction",
                f"Describe what you want to create using this existing {resource}.",
                hint="Example: create ALB, private app tier, and private RDS for a 3-tier app",
            )
            return questions

        if op == "custom" and params.get("custom_instruction") in (None, "", []):
            add_q(
                "custom_instruction",
                f"Enter custom instruction for this {resource}.",
                hint="Example: add 2 tables users and orders, then seed sample rows",
            )
            return questions

        if resource == "rds" and str(params.get("existing_operation", "")).strip().lower() == "custom":
            if params.get("sql_statements") in (None, "", []) and params.get("sql") in (None, "", []):
                add_q(
                    "sql_statements",
                    "Provide SQL statements to run on the selected RDS instance.",
                    hint="Example: CREATE TABLE users (...); INSERT INTO users ...;",
                )
                return questions
            if params.get("db_name") in (None, "", []):
                add_q("db_name", "Which database name should I connect to?", hint="Example: appdb")
                return questions
            if params.get("secret_arn") in (None, "", []) and params.get("db_username") in (None, "", []):
                add_q("db_username", "Provide DB username (or provide secret_arn).")
                return questions
            if params.get("secret_arn") in (None, "", []) and params.get("db_password") in (None, "", []):
                add_q("db_password", "Provide DB password (or provide secret_arn).", qtype="password")
                return questions
            if params.get("bastion_instance_id") in (None, "", []):
                add_q(
                    "bastion_instance_id",
                    "Provide bastion/worker EC2 instance ID (SSM-managed), or type auto to auto-discover.",
                    hint="Example: i-0123456789abcdef0",
                )
                return questions
            if params.get("ensure_network_path") in (None, "", []):
                add_q(
                    "ensure_network_path",
                    "Should I auto-open security group path from bastion to RDS?",
                    qtype="boolean",
                    options=["true", "false"],
                )
                return questions

        return questions

    def _collect_legacy_prerequisite_questions(self, intent: Dict[str, Any]) -> List[Dict[str, Any]]:
        action = (intent or {}).get("action", "")
        resource = (intent or {}).get("resource_type", "")
        params = (intent or {}).get("parameters", {}) if isinstance((intent or {}).get("parameters"), dict) else {}
        questions: List[Dict[str, Any]] = []

        def add_q(variable: str, question: str, qtype: str = "string", hint: Optional[str] = None, options: Optional[List[Any]] = None):
            if variable in params and params.get(variable) not in (None, "", []):
                return
            item: Dict[str, Any] = {"variable": variable, "question": question, "type": qtype}
            if hint:
                item["hint"] = hint
            if options:
                item["options"] = options
            questions.append(item)

        if action == "create" and resource == "rds":
            add_q("instance_type", "Choose DB instance class.", options=["db.t3.micro", "db.t3.small", "db.t4g.micro", "db.m6g.large"])
            add_q("storage_size", "Choose DB storage size (GB).", qtype="number", hint="Example: 20")

        if action == "create" and resource == "eks":
            add_q("node_instance_type", "Choose worker node instance type.", options=["t3.medium", "m5.large", "m6i.large"])
            add_q("node_count", "How many worker nodes?", qtype="number", hint="Example: 2")

        if action == "create" and resource in {"emr", "spark"}:
            add_q("release_label", "Choose EMR release.", options=["emr-7.0.0", "emr-7.1.0", "emr-6.15.0"])
            add_q("master_instance_type", "Choose master node type.", options=["m5.xlarge", "m5.2xlarge"])
            add_q("worker_instance_type", "Choose core node type.", options=["m5.xlarge", "m5.2xlarge"])
            add_q("instance_count", "How many core nodes?", qtype="number", hint="Example: 2")

        if action == "create" and resource == "lambda" and str(params.get("architecture_style", "")).lower() == "serverless":
            add_q("api_type", "Choose API Gateway type for the serverless app.", options=["http-api", "rest-api"])
            add_q("frontend_hosting", "How should frontend be hosted?", options=["s3-cloudfront", "none-api-only"])
            add_q("data_store", "Choose datastore for this app.", options=["dynamodb", "none", "rds"])
            add_q("auth_type", "Choose authentication model.", options=["none", "cognito", "iam-authorizer"])

        return questions

    def _existing_resource_mismatch_reason(self, resource_type: str, resource_id_or_name: str) -> Optional[str]:
        inferred = self._infer_resource_from_identifier(resource_id_or_name)
        expected = str(resource_type or "").strip().lower()
        if not inferred or not expected:
            return None
        if inferred == expected:
            return None
        return (
            f"The value '{resource_id_or_name}' looks like {inferred.upper()}, not {expected.upper()}. "
            f"Please provide an existing {expected.upper()} identifier/name."
        )

    def _infer_resource_from_identifier(self, value: str) -> Optional[str]:
        token = str(value or "").strip().lower()
        if not token:
            return None
        if token.startswith("i-"):
            return "ec2"
        if token.startswith("vpc-"):
            return "vpc"
        if token.startswith("subnet-"):
            return "vpc"
        if token.startswith("sg-"):
            return "security_group"
        if token.startswith("db-"):
            return "rds"
        if token.startswith("lt-"):
            return "ec2"
        if token.startswith("vol-"):
            return "ebs"
        if token.startswith("fs-"):
            return "efs"
        return None

    def _expected_identifier_hint(self, resource_type: str) -> str:
        rt = str(resource_type or "").strip().lower()
        hints = {
            "ec2": "Example EC2 instance ID: i-0123456789abcdef0",
            "vpc": "Example VPC ID: vpc-0123456789abcdef0",
            "security_group": "Example security group ID: sg-0123456789abcdef0",
            "rds": "Example RDS identifier: mydb-instance",
            "ebs": "Example EBS volume ID: vol-0123456789abcdef0",
            "efs": "Example EFS file system ID: fs-01234567",
        }
        return hints.get(rt, f"Provide a valid {rt} identifier/name.")

    def _attach_success_followup_questions(self, context: WorkflowContext) -> WorkflowContext:
        result = context.execution_result or {}
        if not isinstance(result, dict):
            return context
        if not result.get("success"):
            return context
        if result.get("requires_input"):
            return context

        intent = context.intent or {}
        if intent.get("action") != "create" or intent.get("resource_type") != "ec2":
            return context

        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        install_targets = params.get("install_targets") or []
        has_install_request = bool(install_targets or params.get("user_data"))
        if has_install_request:
            return context

        instance_id = result.get("instance_id")
        region = result.get("region") or intent.get("region") or "us-east-1"
        readiness = str(result.get("readiness", "")).lower()
        wait_hint = "Wait ~5 minutes for EC2 checks to pass. I will continue once you provide app details."
        if "instance_status_ok" in readiness:
            wait_hint = "Instance is ready. Provide app details to continue deployment."

        result["requires_input"] = True
        result["question_prompt"] = (
            f"EC2 instance {instance_id} is created. Deployment phase needs app details. "
            f"{wait_hint}"
        )
        result["questions"] = [
            {
                "variable": "app_targets",
                "question": "What applications/tools should I install on this server? (you can provide multiple comma-separated values)",
                "type": "string",
                "hint": "Example: tomcat,nginx,docker",
                "options": ["tomcat", "nginx", "docker", "node", "python", "java", "git"],
            },
            {
                "variable": "app_port",
                "question": "Which application port should I open?",
                "type": "number",
                "hint": "Example: 8080",
            },
            {
                "variable": "public_access",
                "question": "Should this app port be public? (true/false)",
                "type": "boolean",
                "hint": "true for internet-facing, false for private-only",
                "options": ["true", "false"],
            },
            {
                "variable": "custom_commands",
                "question": "Any extra shell commands to run after installation? (optional)",
                "type": "string",
                "hint": "Example: echo hello > /tmp/app.txt",
            },
        ]
        result["continuation"] = {
            "kind": "auto_deploy_ssm",
            "instance_id": instance_id,
            "region": region,
            "recommended_wait_seconds": 300 if "instance_status_ok" not in readiness else 0,
        }

        phases = context.context_data.get("phases", [])
        for phase in phases:
            if phase.get("id") == "deploy_app":
                phase["status"] = "needs_input"
                phase["detail"] = "Waiting for application deployment details from user."
            if phase.get("id") == "validate_health" and phase.get("status") == "completed":
                phase["status"] = "pending"
                phase["detail"] = "Will validate after app deployment."

        context.execution_result = result
        return context
