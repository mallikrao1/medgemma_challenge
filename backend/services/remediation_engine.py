import copy
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class RemediationEngine:
    """Policy-aware remediation planner/executor driven by JSON rules."""

    def __init__(
        self,
        rules_path: Optional[str] = None,
        enabled: bool = True,
        preview_only: bool = False,
        max_attempts: int = 2,
    ):
        self.enabled = bool(enabled)
        self.preview_only = bool(preview_only)
        self.max_attempts = max(1, int(max_attempts or 2))
        self.rules_path = str(rules_path or "").strip()
        self.defaults: Dict[str, Any] = {
            "approval_scope": "request_run",
            "retry_strategy": {
                "mode": "wait_then_retry",
                "max_attempts": self.max_attempts,
                "backoff_seconds": [15, 30, 60],
            },
            "safety": {"destructive": False, "requires_admin": False},
        }
        self.rules: List[Dict[str, Any]] = []
        self._load_rules()

    def _load_rules(self):
        self.rules = []
        if not self.rules_path:
            return
        try:
            p = Path(self.rules_path).expanduser()
            if not p.exists():
                return
            payload = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(payload.get("defaults"), dict):
                merged_defaults = copy.deepcopy(self.defaults)
                merged_defaults.update(payload["defaults"])
                self.defaults = merged_defaults
            if isinstance(payload.get("rules"), list):
                self.rules = [r for r in payload["rules"] if isinstance(r, dict)]
        except Exception:
            self.rules = []

    def _collect_errors(self, payload: Any) -> List[str]:
        errors: List[str] = []
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, str) and err.strip():
                errors.append(err.strip())
            for v in payload.values():
                errors.extend(self._collect_errors(v))
        elif isinstance(payload, list):
            for item in payload:
                errors.extend(self._collect_errors(item))
        return errors

    def _matches_rule(
        self,
        rule: Dict[str, Any],
        resource_type: str,
        action: str,
        error_text: str,
    ) -> bool:
        rule_resource = str(rule.get("resource_type") or "*").strip().lower()
        rule_action = str(rule.get("action") or "*").strip().lower()
        if rule_resource not in {"*", resource_type}:
            return False
        if rule_action not in {"*", action}:
            return False
        matcher = rule.get("error_match", {}) if isinstance(rule.get("error_match"), dict) else {}
        contains_any = [str(x).lower() for x in matcher.get("contains_any", []) if str(x).strip()]
        regex_any = [str(x) for x in matcher.get("regex_any", []) if str(x).strip()]
        if contains_any and not any(token in error_text for token in contains_any):
            return False
        if regex_any:
            ok = False
            for pattern in regex_any:
                try:
                    if re.search(pattern, error_text, flags=re.IGNORECASE):
                        ok = True
                        break
                except Exception:
                    continue
            if not ok:
                return False
        return bool(contains_any or regex_any)

    def _context_values(
        self,
        intent: Dict[str, Any],
        execution_result: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ctx = context or {}
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        values: Dict[str, Any] = {
            "environment": ctx.get("environment") or "dev",
            "region": intent.get("region") or ctx.get("region"),
            "resource_type": intent.get("resource_type"),
            "action": intent.get("action"),
            "resource_name": intent.get("resource_name"),
            "instance_id": execution_result.get("instance_id") or params.get("instance_id") or intent.get("resource_name"),
            "bastion_instance_id": execution_result.get("bastion_instance_id") or params.get("bastion_instance_id"),
            "db_instance_id": execution_result.get("db_instance_id") or params.get("db_instance_id") or intent.get("resource_name"),
        }
        for k, v in params.items():
            if k not in values:
                values[k] = v
        return values

    def _format_value(self, template: Any, values: Dict[str, Any]) -> Any:
        if isinstance(template, str):
            try:
                return template.format_map(values)
            except Exception:
                return template
        if isinstance(template, dict):
            return {k: self._format_value(v, values) for k, v in template.items()}
        if isinstance(template, list):
            return [self._format_value(item, values) for item in template]
        return template

    def build_plan(
        self,
        intent: Dict[str, Any],
        execution_result: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        if not isinstance(intent, dict) or not isinstance(execution_result, dict):
            return None
        if execution_result.get("success"):
            return None

        errors = self._collect_errors(execution_result)
        if not errors:
            return None
        error_text = " | ".join(errors).lower()
        action = str(intent.get("action") or "").strip().lower()
        resource_type = str(intent.get("resource_type") or "").strip().lower()
        if not action or not resource_type:
            return None

        selected = None
        for rule in self.rules:
            if self._matches_rule(rule, resource_type, action, error_text):
                selected = rule
                break
        if not selected:
            return None

        values = self._context_values(intent, execution_result, context=context)
        run_id = f"rem-{uuid.uuid4().hex[:12]}"
        actions = selected.get("actions", []) if isinstance(selected.get("actions"), list) else []
        planned_actions: List[Dict[str, Any]] = []
        for action_item in actions:
            if not isinstance(action_item, dict):
                continue
            planned_actions.append(
                {
                    "type": action_item.get("type"),
                    "params": self._format_value(action_item.get("params", {}), values),
                }
            )

        approval_template = str(selected.get("approval_message_template") or "I can apply automatic remediation and retry.")
        reason = self._format_value(approval_template, values)
        human_actions = selected.get("human_actions", []) if isinstance(selected.get("human_actions"), list) else []
        required_permissions = [str(p) for p in selected.get("required_permissions", []) if str(p).strip()]
        retry_strategy = copy.deepcopy(self.defaults.get("retry_strategy", {}))
        if isinstance(selected.get("retry_strategy"), dict):
            retry_strategy.update(selected["retry_strategy"])
        safety = copy.deepcopy(self.defaults.get("safety", {}))
        if isinstance(selected.get("safety"), dict):
            safety.update(selected["safety"])

        plan = {
            "required": True,
            "run_id": run_id,
            "rule_id": selected.get("id"),
            "resource_type": resource_type,
            "action": action,
            "reason": str(reason),
            "actions": human_actions,
            "required_permissions": required_permissions,
            "approval_scope": self.defaults.get("approval_scope", "request_run"),
            "execution_actions": planned_actions,
            "retry_strategy": retry_strategy,
            "safety": safety,
            "error_excerpt": errors[0],
        }
        return plan

    async def execute_plan(
        self,
        remediation_plan: Dict[str, Any],
        aws_executor: Any,
        auth_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"success": False, "error": "Auto-remediation is disabled."}
        if not isinstance(remediation_plan, dict):
            return {"success": False, "error": "Invalid remediation plan."}
        execution_actions = remediation_plan.get("execution_actions", [])
        if not isinstance(execution_actions, list):
            execution_actions = []
        if not execution_actions:
            return {
                "success": False,
                "requires_manual": True,
                "error": "No executable auto-remediation actions are defined for this issue.",
            }
        if self.preview_only:
            return {
                "success": False,
                "preview_only": True,
                "error": "Auto-remediation preview mode is enabled. Execution is blocked.",
            }

        results: List[Dict[str, Any]] = []
        for step in execution_actions:
            step_type = str(step.get("type") or "").strip()
            params = step.get("params", {}) if isinstance(step.get("params"), dict) else {}
            if not step_type:
                continue
            try:
                outcome = await self._run_step(step_type, params, aws_executor, auth_context=auth_context or {})
                results.append({"type": step_type, "success": bool(outcome.get("success")), "result": outcome})
                if not outcome.get("success"):
                    return {
                        "success": False,
                        "error": outcome.get("error") or f"Remediation step '{step_type}' failed.",
                        "steps": results,
                    }
            except Exception as e:
                return {
                    "success": False,
                    "error": str(e),
                    "steps": results,
                }

        return {"success": True, "steps": results}

    async def _run_step(
        self,
        step_type: str,
        params: Dict[str, Any],
        aws_executor: Any,
        auth_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if step_type == "ensure_ssm_prerequisites_for_instance":
            return await aws_executor.ensure_ssm_prerequisites_for_instance(
                instance_id=str(params.get("instance_id") or "").strip(),
                environment=str(params.get("environment") or (auth_context or {}).get("environment") or "dev"),
                role_model=str(params.get("role_model") or "shared"),
                wait_seconds=int(params.get("wait_seconds") or 300),
            )
        if step_type == "ensure_managed_instance_profile":
            return await aws_executor.ensure_managed_instance_profile(
                environment=str(params.get("environment") or (auth_context or {}).get("environment") or "dev"),
                role_model=str(params.get("role_model") or "shared"),
            )
        if step_type == "attach_instance_profile":
            return await aws_executor.attach_instance_profile(
                instance_id=str(params.get("instance_id") or "").strip(),
                profile_name=str(params.get("profile_name") or "").strip(),
            )
        if step_type == "wait_for_ssm_registration":
            return await aws_executor.wait_for_ssm_registration(
                instance_id=str(params.get("instance_id") or "").strip(),
                timeout_seconds=int(params.get("timeout_seconds") or 300),
            )
        if step_type == "ensure_service_linked_role":
            return await aws_executor.ensure_service_linked_role(
                service_name=str(params.get("service_name") or "").strip()
            )
        if step_type == "ensure_eks_cluster_role":
            return await aws_executor.ensure_eks_cluster_role(
                environment=str(params.get("environment") or (auth_context or {}).get("environment") or "dev"),
                cluster_name=str(params.get("cluster_name") or "").strip() or None,
                tags=params.get("tags") if isinstance(params.get("tags"), dict) else None,
            )
        if step_type == "ensure_service_role":
            return await aws_executor.ensure_service_role(
                service_slug=str(params.get("service_slug") or "").strip(),
                service_principal=str(params.get("service_principal") or "").strip(),
                policy_arns=[str(p) for p in (params.get("policy_arns") or []) if str(p).strip()],
                environment=str(params.get("environment") or (auth_context or {}).get("environment") or "dev"),
                role_name=str(params.get("role_name") or "").strip() or None,
                tags=params.get("tags") if isinstance(params.get("tags"), dict) else None,
            )
        if step_type == "ensure_iam_policy_attached":
            return await aws_executor.ensure_iam_policy_attached(
                role_name=str(params.get("role_name") or "").strip(),
                policy_arn=str(params.get("policy_arn") or "").strip(),
            )
        if step_type == "ensure_security_group_ingress":
            return await aws_executor.ensure_security_group_ingress(
                group_id=str(params.get("group_id") or "").strip(),
                port=int(params.get("port") or 0),
                protocol=str(params.get("protocol") or "tcp"),
                cidr=str(params.get("cidr") or "0.0.0.0/0"),
            )
        if step_type == "ensure_bucket_public_access_compliance":
            return await aws_executor.ensure_bucket_public_access_compliance(
                bucket_name=str(params.get("bucket_name") or "").strip(),
                allow_public=bool(params.get("allow_public")),
            )
        if step_type == "ensure_endpoint_network_association":
            return await aws_executor.ensure_endpoint_network_association(
                service_name=str(params.get("service_name") or "").strip(),
                resource_id=str(params.get("resource_id") or "").strip(),
                subnet_ids=params.get("subnet_ids"),
                security_group_ids=params.get("security_group_ids"),
            )
        return {"success": False, "error": f"Unsupported remediation action '{step_type}'."}
