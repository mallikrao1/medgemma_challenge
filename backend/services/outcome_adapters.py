import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yaml = None


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return ""


class OutcomeValidationRegistry:
    """Service/operation-specific outcome validators with generic fallbacks.

    Supports admin-configured adapter rules from JSON or YAML.
    """

    _NOT_FOUND_HINTS = (
        "not found",
        "does not exist",
        "resource not found",
        "invalid",
        "no such",
    )

    _PENDING_HINTS = (
        "creating",
        "pending",
        "initializing",
        "starting",
        "provisioning",
        "inprogress",
    )

    _READY_HINTS = (
        "available",
        "active",
        "running",
        "ok",
        "ready",
        "completed",
        "enabled",
        "issued",
        "inservice",
        "deployed",
    )

    _DEFAULT_ENDPOINT_KEYS = (
        "website_url",
        "invoke_url",
        "endpoint",
        "url",
        "api_url",
        "app_url",
    )

    def __init__(self, aws_executor, rules_path: Optional[str] = None):
        self.aws_executor = aws_executor
        self._adapters: Dict[str, Callable] = {}
        self._aliases: Dict[str, str] = self._default_alias_map()
        self._identifier_key_map: Dict[str, tuple] = self._default_identifier_map()
        self._endpoint_keys_by_resource: Dict[str, tuple] = {}
        self._endpoint_templates: Dict[str, List[str]] = {}
        self._config_phase_hints: Dict[str, Dict[str, Any]] = {}
        self._config_status_hints: Dict[str, Dict[str, Any]] = {}
        self.rules_source: Optional[str] = None

        self._register_defaults()
        self._load_admin_rules(rules_path)

    def _register_defaults(self):
        for resource in self._identifier_key_map.keys():
            self._adapters[resource] = self._validate_generic

        self._adapters["s3"] = self._validate_s3
        self._adapters["ec2"] = self._validate_ec2
        self._adapters["eks"] = self._validate_eks
        self._adapters["apigateway"] = self._validate_apigateway
        self._adapters["elb"] = self._validate_elb
        self._adapters["cloudfront"] = self._validate_cloudfront
        self._adapters["wellarchitected"] = self._validate_wellarchitected

    async def validate(self, intent: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        action = str(result.get("action") or intent.get("action") or "").strip().lower()
        resource_type = self._normalize_resource_type(
            str(result.get("resource_type") or intent.get("resource_type") or "").strip().lower()
        )

        checks: List[Dict[str, Any]] = []
        phase_hints: Dict[str, Any] = {}

        def add_check(
            check_type: str,
            target: str,
            state: str,
            detail: Optional[str] = None,
            status_code: Optional[int] = None,
        ):
            normalized_state = state if state in {"pass", "fail", "pending", "skipped"} else "fail"
            item: Dict[str, Any] = {
                "type": check_type,
                "target": target,
                "state": normalized_state,
                "success": normalized_state == "pass",
            }
            if detail:
                item["detail"] = detail
            if status_code is not None:
                item["status_code"] = int(status_code)
            checks.append(item)

        adapter = self._adapters.get(resource_type, self._validate_generic)
        await adapter(intent, result, action, resource_type, add_check, phase_hints)

        self._apply_config_phase_hints(resource_type, action, phase_hints)

        endpoints = self._collect_endpoint_candidates(intent, result, resource_type)
        await self._validate_http_endpoints(endpoints, action, add_check, phase_hints)

        if not checks:
            return {"performed": False, "checks": [], "phase_hints": phase_hints}

        summary = {
            "total": len(checks),
            "passed": len([c for c in checks if c.get("state") == "pass"]),
            "failed": len([c for c in checks if c.get("state") == "fail"]),
            "pending": len([c for c in checks if c.get("state") == "pending"]),
            "skipped": len([c for c in checks if c.get("state") == "skipped"]),
        }

        out = {
            "performed": True,
            "adapter": resource_type or "generic",
            "checks": checks,
            "summary": summary,
            "phase_hints": phase_hints,
        }
        if self.rules_source:
            out["rules_source"] = self.rules_source
        return out

    async def _validate_generic(
        self,
        intent: Dict[str, Any],
        result: Dict[str, Any],
        action: str,
        resource_type: str,
        add_check: Callable[..., None],
        phase_hints: Dict[str, Any],
    ):
        identifier = self._extract_identifier(intent, result, resource_type)
        if action in {"create", "update"}:
            if identifier:
                described = await self._describe(resource_type, identifier)
                if described is not None:
                    self._add_describe_check(add_check, resource_type, identifier, described)
                    return
            listed = await self._list(resource_type)
            if listed is not None:
                if identifier:
                    state = "pass" if self._contains_identifier(listed, identifier) else "fail"
                    add_check("resource_exists", identifier, state)
                else:
                    count = listed.get("count")
                    detail = f"List returned {count} items." if isinstance(count, int) else "List response received."
                    add_check("resource_exists", resource_type, "pass", detail=detail)
                return

        if action == "delete":
            if not identifier:
                add_check("resource_deleted", resource_type, "skipped", detail="Resource identifier unavailable for delete verification.")
                return
            described = await self._describe(resource_type, identifier)
            if described is not None:
                if described.get("success"):
                    add_check("resource_deleted", identifier, "fail", detail="Resource still exists.")
                else:
                    state = "pass" if self._is_not_found_error(described.get("error")) else "fail"
                    add_check("resource_deleted", identifier, state, detail=described.get("error"))
                return
            listed = await self._list(resource_type)
            if listed is not None:
                exists = self._contains_identifier(listed, identifier)
                add_check("resource_deleted", identifier, "fail" if exists else "pass")
                return

        if action == "list":
            count = result.get("count")
            if isinstance(count, int):
                add_check("list_response", resource_type, "pass", detail=f"Returned {count} items.")
            else:
                add_check("list_response", resource_type, "pass")
            return

        if action == "describe":
            if result.get("success"):
                add_check("describe_response", identifier or resource_type, "pass")
            else:
                add_check("describe_response", identifier or resource_type, "fail", detail=result.get("error"))

    async def _validate_s3(
        self,
        intent: Dict[str, Any],
        result: Dict[str, Any],
        action: str,
        resource_type: str,
        add_check: Callable[..., None],
        phase_hints: Dict[str, Any],
    ):
        await self._validate_generic(intent, result, action, resource_type, add_check, phase_hints)
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        website_requested = bool(
            result.get("website_configuration")
            or params.get("website_configuration")
            or params.get("website_enabled")
        )
        if website_requested and action in {"create", "update"}:
            phase_hints["deploy_completed"] = True
            phase_hints["deploy_detail"] = "Static website configuration applied."

    async def _validate_ec2(
        self,
        intent: Dict[str, Any],
        result: Dict[str, Any],
        action: str,
        resource_type: str,
        add_check: Callable[..., None],
        phase_hints: Dict[str, Any],
    ):
        if action not in {"create", "update"}:
            await self._validate_generic(intent, result, action, resource_type, add_check, phase_hints)
            return

        instance_id = str(result.get("instance_id") or "").strip()
        if not instance_id:
            await self._validate_generic(intent, result, action, resource_type, add_check, phase_hints)
            return

        readiness = await self.aws_executor.check_ec2_instance_readiness(instance_id)
        if not readiness.get("success"):
            add_check("ec2_status", instance_id, "fail", detail=readiness.get("error"))
            return
        if readiness.get("ready"):
            add_check("ec2_status", instance_id, "pass", detail="Instance and system status checks are ok.")
            phase_hints["health_detail"] = "Instance status checks passed."
        else:
            detail = (
                f"state={readiness.get('state')}, "
                f"instance={readiness.get('instance_status')}, "
                f"system={readiness.get('system_status')}"
            )
            add_check("ec2_status", instance_id, "pending", detail=detail)
            phase_hints["health_detail"] = "Instance is still initializing."

    async def _validate_eks(
        self,
        intent: Dict[str, Any],
        result: Dict[str, Any],
        action: str,
        resource_type: str,
        add_check: Callable[..., None],
        phase_hints: Dict[str, Any],
    ):
        cluster_name = str(result.get("cluster_name") or intent.get("resource_name") or "").strip()
        if not cluster_name:
            await self._validate_generic(intent, result, action, resource_type, add_check, phase_hints)
            return

        describe = await self._describe(resource_type, cluster_name)
        if describe is None:
            await self._validate_generic(intent, result, action, resource_type, add_check, phase_hints)
            return
        if describe.get("success"):
            status = str(describe.get("status") or "").strip().lower()
            state = "pass" if "active" in status else "pending" if status else "pass"
            add_check("eks_describe", cluster_name, state, detail=f"status={describe.get('status')}")
            result["cluster_details"] = {
                "cluster_name": describe.get("cluster_name"),
                "version": describe.get("version"),
                "status": describe.get("status"),
                "endpoint": describe.get("endpoint"),
            }
            nodegroups: List[str] = []
            try:
                eks = self.aws_executor._client("eks")
                ng_resp = eks.list_nodegroups(clusterName=cluster_name)
                nodegroups = list(ng_resp.get("nodegroups") or [])
            except Exception:
                nodegroups = []
            result["nodegroups"] = nodegroups
            add_check(
                "eks_nodegroups",
                cluster_name,
                "pass" if nodegroups else "pending",
                detail=f"count={len(nodegroups)}",
            )

            app_endpoint = (
                result.get("public_url")
                or result.get("website_url")
                or result.get("app_url")
                or result.get("ingress_url")
            )
            phase_hints["deploy_completed"] = bool(app_endpoint)
            if app_endpoint:
                phase_hints["deploy_detail"] = "Application workload is deployed and endpoint is available."
            elif state == "pending":
                phase_hints["deploy_detail"] = "Waiting for EKS cluster to become active before app deployment."
            elif nodegroups:
                phase_hints["deploy_detail"] = "Cluster and node groups are ready. App deployment is pending."
            else:
                phase_hints["deploy_detail"] = "Cluster is active but node groups are not ready yet."

            if state == "pending":
                phase_hints["health_detail"] = "EKS cluster is still becoming active."
            elif not nodegroups:
                phase_hints["health_detail"] = "EKS cluster active; waiting for node group readiness."
            elif not app_endpoint:
                phase_hints["health_detail"] = "EKS infrastructure is ready. Workload deployment is still pending."
        else:
            add_check("eks_describe", cluster_name, "fail", detail=describe.get("error"))

    async def _validate_apigateway(
        self,
        intent: Dict[str, Any],
        result: Dict[str, Any],
        action: str,
        resource_type: str,
        add_check: Callable[..., None],
        phase_hints: Dict[str, Any],
    ):
        await self._validate_generic(intent, result, action, resource_type, add_check, phase_hints)
        api_id = str(result.get("api_id") or "").strip()
        region = str(result.get("region") or intent.get("region") or getattr(self.aws_executor, "region", "us-east-1")).strip()
        if api_id and action in {"create", "update"}:
            params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
            stage = str(params.get("stage_name") or params.get("stage") or "").strip("/")
            invoke = f"https://{api_id}.execute-api.{region}.amazonaws.com"
            if stage:
                invoke = f"{invoke}/{stage}"
            result.setdefault("invoke_url", invoke)
            phase_hints["deploy_completed"] = True
            phase_hints["deploy_detail"] = "API endpoint provisioned."

    async def _validate_elb(
        self,
        intent: Dict[str, Any],
        result: Dict[str, Any],
        action: str,
        resource_type: str,
        add_check: Callable[..., None],
        phase_hints: Dict[str, Any],
    ):
        await self._validate_generic(intent, result, action, resource_type, add_check, phase_hints)
        dns_name = str(result.get("dns_name") or "").strip()
        if dns_name and action in {"create", "update"}:
            result.setdefault("url", f"http://{dns_name}")
            phase_hints["deploy_completed"] = True
            phase_hints["deploy_detail"] = "Load balancer endpoint is available."

    async def _validate_cloudfront(
        self,
        intent: Dict[str, Any],
        result: Dict[str, Any],
        action: str,
        resource_type: str,
        add_check: Callable[..., None],
        phase_hints: Dict[str, Any],
    ):
        await self._validate_generic(intent, result, action, resource_type, add_check, phase_hints)
        domain = str(result.get("domain_name") or "").strip()
        if domain and action in {"create", "update"}:
            result.setdefault("url", f"https://{domain}")
            phase_hints["deploy_completed"] = True
            phase_hints["deploy_detail"] = "CloudFront endpoint is available."

    async def _validate_wellarchitected(
        self,
        intent: Dict[str, Any],
        result: Dict[str, Any],
        action: str,
        resource_type: str,
        add_check: Callable[..., None],
        phase_hints: Dict[str, Any],
    ):
        await self._validate_generic(intent, result, action, resource_type, add_check, phase_hints)
        if action in {"create", "update"}:
            phase_hints["deploy_completed"] = True
            phase_hints["deploy_detail"] = "Well-Architected workload changes applied."

    async def _validate_http_endpoints(
        self,
        endpoints: List[str],
        action: str,
        add_check: Callable[..., None],
        phase_hints: Dict[str, Any],
    ):
        if not endpoints:
            return

        if action in {"create", "update"}:
            phase_hints.setdefault("deploy_completed", True)
            phase_hints.setdefault("deploy_detail", "Application endpoint is available.")

        for endpoint in endpoints[:4]:
            try:
                request = urllib.request.Request(endpoint, method="GET")
                with urllib.request.urlopen(request, timeout=12) as response:
                    status = int(getattr(response, "status", 200))
                    if status < 400:
                        add_check("http_get", endpoint, "pass", status_code=status)
                    elif status < 500:
                        # 4xx often means endpoint is live but route/auth is restricted.
                        add_check("http_get", endpoint, "pass", detail=f"Reachable with HTTP {status}.", status_code=status)
                    else:
                        add_check("http_get", endpoint, "fail", status_code=status)
            except urllib.error.HTTPError as err:
                status_code = int(getattr(err, "code", 0) or 0)
                if 400 <= status_code < 500:
                    add_check("http_get", endpoint, "pass", detail=str(err), status_code=status_code)
                else:
                    state = "pending" if self._looks_transient_error(str(err)) else "fail"
                    add_check("http_get", endpoint, state, detail=str(err), status_code=status_code or None)
            except Exception as err:
                text = str(err)
                state = "pending" if self._looks_transient_error(text) else "fail"
                add_check("http_get", endpoint, state, detail=text)

    async def _describe(self, resource_type: str, identifier: str) -> Optional[Dict[str, Any]]:
        if not identifier:
            return None
        if not hasattr(self.aws_executor, f"_describe_{resource_type}"):
            return None
        return await self.aws_executor.execute(
            action="describe",
            resource_type=resource_type,
            resource_name=identifier,
            parameters={},
            tags={},
        )

    async def _list(self, resource_type: str) -> Optional[Dict[str, Any]]:
        if not hasattr(self.aws_executor, f"_list_{resource_type}"):
            return None
        return await self.aws_executor.execute(
            action="list",
            resource_type=resource_type,
            resource_name=None,
            parameters={},
            tags={},
        )

    def _add_describe_check(
        self,
        add_check: Callable[..., None],
        resource_type: str,
        identifier: str,
        described: Dict[str, Any],
    ):
        if not described.get("success"):
            add_check("resource_describe", identifier, "fail", detail=described.get("error"))
            return
        status = self._extract_status(described, resource_type)
        if status:
            state = self._classify_status(resource_type, status)
            add_check("resource_describe", identifier, state, detail=f"status={status}")
            return
        add_check("resource_describe", identifier, "pass")

    def _extract_status(self, payload: Dict[str, Any], resource_type: str) -> Optional[str]:
        status_keys = ["status", "state", "lifecycle_state", "cluster_status", "instance_status"]
        custom = self._config_status_hints.get(resource_type, {}) if isinstance(self._config_status_hints.get(resource_type), dict) else {}
        for key in self._normalize_string_list(custom.get("status_keys")):
            if key and key not in status_keys:
                status_keys.append(key)
        for key in status_keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _classify_status(self, resource_type: str, status: str) -> str:
        status_text = str(status or "").lower().replace(" ", "")
        custom = self._config_status_hints.get(resource_type, {}) if isinstance(self._config_status_hints.get(resource_type), dict) else {}

        pending_tokens = list(self._PENDING_HINTS)
        for token in self._normalize_string_list(custom.get("pending_values")):
            if token:
                pending_tokens.append(token.lower().replace(" ", ""))

        ready_tokens = list(self._READY_HINTS)
        for token in self._normalize_string_list(custom.get("ready_values")):
            if token:
                ready_tokens.append(token.lower().replace(" ", ""))

        if any(token in status_text for token in pending_tokens):
            return "pending"
        if any(token in status_text for token in ready_tokens):
            return "pass"
        return "pass"

    def _collect_endpoint_candidates(self, intent: Dict[str, Any], result: Dict[str, Any], resource_type: str) -> List[str]:
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        candidates: List[str] = []

        endpoint_keys = list(self._DEFAULT_ENDPOINT_KEYS)
        for key in self._endpoint_keys_by_resource.get(resource_type, ()):  # admin-configured
            if key and key not in endpoint_keys:
                endpoint_keys.append(key)

        for source in (result, params):
            for key in endpoint_keys:
                value = source.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    candidates.append(value)

        if resource_type == "s3":
            website_requested = bool(
                result.get("website_configuration")
                or params.get("website_configuration")
                or params.get("website_enabled")
            )
            bucket_name = result.get("bucket_name") or intent.get("resource_name")
            if website_requested and bucket_name:
                region = result.get("region") or intent.get("region") or getattr(self.aws_executor, "region", "us-east-1")
                website_url = f"http://{bucket_name}.s3-website-{region}.amazonaws.com"
                result.setdefault("website_url", website_url)
                candidates.append(website_url)

        template_data = self._build_template_data(intent, result)
        for template in self._endpoint_templates.get(resource_type, []):
            rendered = self._render_template(template, template_data)
            if rendered and rendered.startswith(("http://", "https://")):
                candidates.append(rendered)

        deduped: List[str] = []
        seen = set()
        for url in candidates:
            if url not in seen:
                deduped.append(url)
                seen.add(url)
        return deduped

    def _build_template_data(self, intent: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        data: Dict[str, Any] = {}
        data.update(params)
        data.update(intent)
        data.update(result)
        data.setdefault("region", result.get("region") or intent.get("region") or getattr(self.aws_executor, "region", "us-east-1"))
        data.setdefault("resource_name", intent.get("resource_name") or result.get("name") or "")
        data.setdefault("action", result.get("action") or intent.get("action") or "")
        return data

    def _render_template(self, template: str, data: Dict[str, Any]) -> str:
        text = str(template or "").strip()
        if not text:
            return ""
        try:
            rendered = text.format_map(_SafeFormatDict(data))
        except Exception:
            return ""
        return str(rendered).strip()

    def _extract_identifier(self, intent: Dict[str, Any], result: Dict[str, Any], resource_type: str) -> Optional[str]:
        key_map = self._identifier_key_map

        for key in key_map.get(resource_type, ()): 
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        for key in key_map.get(resource_type, ()): 
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        fallback = intent.get("resource_name")
        if isinstance(fallback, str) and fallback.strip():
            return fallback.strip()
        return None

    def _contains_identifier(self, payload: Dict[str, Any], identifier: Optional[str]) -> bool:
        if not identifier:
            return False
        normalized = identifier.strip().lower()
        if not normalized:
            return False
        strings = self._collect_strings(payload)
        for item in strings:
            low = item.lower()
            if low == normalized:
                return True
            if low.endswith("/" + normalized):
                return True
            if normalized.startswith("arn:") and low == normalized:
                return True
        return False

    def _collect_strings(self, node: Any) -> List[str]:
        out: List[str] = []
        if isinstance(node, dict):
            for value in node.values():
                out.extend(self._collect_strings(value))
            return out
        if isinstance(node, list):
            for item in node:
                out.extend(self._collect_strings(item))
            return out
        if isinstance(node, str):
            cleaned = node.strip()
            if cleaned:
                out.append(cleaned)
        return out

    def _is_not_found_error(self, text: Any) -> bool:
        value = str(text or "").strip().lower()
        return any(token in value for token in self._NOT_FOUND_HINTS)

    def _looks_transient_error(self, text: str) -> bool:
        value = (text or "").lower()
        return any(token in value for token in ("timed out", "name or service not known", "temporary failure", "connection reset"))

    def _normalize_resource_type(self, resource_type: str) -> str:
        return self._aliases.get(resource_type, resource_type)

    def _normalize_string_list(self, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        cleaned = str(value).strip()
        return [cleaned] if cleaned else []

    def _apply_config_phase_hints(self, resource_type: str, action: str, phase_hints: Dict[str, Any]):
        rule = self._config_phase_hints.get(resource_type)
        if not isinstance(rule, dict):
            return

        actions = self._normalize_string_list(rule.get("deploy_completed_actions"))
        normalized_actions = {item.lower() for item in actions}
        if normalized_actions and (action in normalized_actions or "*" in normalized_actions):
            deploy_completed = rule.get("deploy_completed", True)
            if isinstance(deploy_completed, bool):
                phase_hints["deploy_completed"] = deploy_completed
            if rule.get("deploy_detail"):
                phase_hints["deploy_detail"] = str(rule.get("deploy_detail"))

        action_rules = rule.get("actions")
        if isinstance(action_rules, dict):
            selected = action_rules.get(action) or action_rules.get("*")
            if isinstance(selected, dict):
                if isinstance(selected.get("deploy_completed"), bool):
                    phase_hints["deploy_completed"] = bool(selected.get("deploy_completed"))
                if selected.get("deploy_detail"):
                    phase_hints["deploy_detail"] = str(selected.get("deploy_detail"))
                if selected.get("health_detail"):
                    phase_hints["health_detail"] = str(selected.get("health_detail"))

        if rule.get("health_detail") and "health_detail" not in phase_hints:
            phase_hints["health_detail"] = str(rule.get("health_detail"))

    def _load_admin_rules(self, rules_path: Optional[str]):
        data, source = self._read_rules_file(rules_path)
        if not isinstance(data, dict):
            return
        self.rules_source = source

        self._apply_rule_block(data)

        services = data.get("services")
        if isinstance(services, dict):
            for service_name, rule in services.items():
                if isinstance(rule, dict):
                    self._apply_service_rule(service_name, rule)

        for resource in set(self._identifier_key_map.keys()) | set(self._endpoint_keys_by_resource.keys()) | set(self._endpoint_templates.keys()) | set(self._config_phase_hints.keys()):
            self._ensure_adapter(resource)

    def _read_rules_file(self, rules_path: Optional[str]) -> tuple:
        candidates: List[Path] = []

        explicit = str(rules_path or "").strip() or str(os.environ.get("OUTCOME_ADAPTER_RULES_PATH") or "").strip()
        if explicit:
            candidates.append(Path(explicit).expanduser())
        else:
            base = Path(__file__).resolve().parent.parent
            candidates.extend([
                base / "config" / "outcome_adapter_rules.json",
                base / "config" / "outcome_adapter_rules.yaml",
                base / "config" / "outcome_adapter_rules.yml",
            ])

        for candidate in candidates:
            if not candidate.exists() or not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except Exception:
                continue

            suffix = candidate.suffix.lower()
            if suffix == ".json":
                try:
                    return json.loads(text), str(candidate)
                except Exception as exc:
                    print(f"  Outcome adapter rules JSON parse failed: {exc}")
                    return {}, str(candidate)

            if suffix in {".yaml", ".yml"}:
                if yaml is None:
                    print("  Outcome adapter rules YAML file found but PyYAML is not installed. Using defaults.")
                    return {}, str(candidate)
                try:
                    return yaml.safe_load(text) or {}, str(candidate)
                except Exception as exc:
                    print(f"  Outcome adapter rules YAML parse failed: {exc}")
                    return {}, str(candidate)

        return {}, None

    def _apply_rule_block(self, data: Dict[str, Any]):
        aliases = data.get("aliases")
        if isinstance(aliases, dict):
            for alias, target in aliases.items():
                alias_key = str(alias).strip().lower()
                target_key = str(target).strip().lower()
                if alias_key and target_key:
                    self._aliases[alias_key] = target_key

        identifier_keys = data.get("identifier_keys")
        if isinstance(identifier_keys, dict):
            for resource, keys in identifier_keys.items():
                self._set_identifier_keys(resource, keys)

        endpoint_keys = data.get("endpoint_keys")
        if isinstance(endpoint_keys, dict):
            for resource, keys in endpoint_keys.items():
                self._set_endpoint_keys(resource, keys)

        endpoint_templates = data.get("endpoint_templates")
        if isinstance(endpoint_templates, dict):
            for resource, templates in endpoint_templates.items():
                self._set_endpoint_templates(resource, templates)

        phase_hints = data.get("phase_hints")
        if isinstance(phase_hints, dict):
            for resource, rule in phase_hints.items():
                self._set_phase_hints(resource, rule)

        status_hints = data.get("status_hints")
        if isinstance(status_hints, dict):
            for resource, rule in status_hints.items():
                self._set_status_hints(resource, rule)

    def _apply_service_rule(self, service_name: str, rule: Dict[str, Any]):
        resource = self._normalize_resource_type_name(service_name)

        alias = rule.get("alias")
        if isinstance(alias, str) and alias.strip():
            self._aliases[alias.strip().lower()] = resource

        aliases = rule.get("aliases")
        if isinstance(aliases, list):
            for alias_item in aliases:
                alias_text = str(alias_item).strip().lower()
                if alias_text:
                    self._aliases[alias_text] = resource

        self._set_identifier_keys(resource, rule.get("identifier_keys"))
        self._set_endpoint_keys(resource, rule.get("endpoint_keys"))
        self._set_endpoint_templates(resource, rule.get("endpoint_templates"))
        self._set_phase_hints(resource, rule.get("phase_hints"))
        self._set_status_hints(resource, rule.get("status_hints"))

        self._ensure_adapter(resource)

    def _set_identifier_keys(self, resource: Any, keys: Any):
        resource_key = self._normalize_resource_type_name(resource)
        values = tuple(self._normalize_string_list(keys))
        if not resource_key or not values:
            return
        self._identifier_key_map[resource_key] = values

    def _set_endpoint_keys(self, resource: Any, keys: Any):
        resource_key = self._normalize_resource_type_name(resource)
        values = tuple(self._normalize_string_list(keys))
        if not resource_key or not values:
            return
        self._endpoint_keys_by_resource[resource_key] = values

    def _set_endpoint_templates(self, resource: Any, templates: Any):
        resource_key = self._normalize_resource_type_name(resource)
        values = self._normalize_string_list(templates)
        if not resource_key or not values:
            return
        self._endpoint_templates[resource_key] = values

    def _set_phase_hints(self, resource: Any, rule: Any):
        resource_key = self._normalize_resource_type_name(resource)
        if not resource_key or not isinstance(rule, dict):
            return
        self._config_phase_hints[resource_key] = dict(rule)

    def _set_status_hints(self, resource: Any, rule: Any):
        resource_key = self._normalize_resource_type_name(resource)
        if not resource_key or not isinstance(rule, dict):
            return
        self._config_status_hints[resource_key] = dict(rule)

    def _ensure_adapter(self, resource: str):
        if resource and resource not in self._adapters:
            self._adapters[resource] = self._validate_generic

    def _normalize_resource_type_name(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        return self._aliases.get(text, text)

    def _default_alias_map(self) -> Dict[str, str]:
        return {
            "spark": "emr",
            "database": "rds",
            "natgateway": "nat_gateway",
            "internetgateway": "internet_gateway",
            "securitygroup": "security_group",
            "cloudwatchlogs": "log_group",
            "logs": "log_group",
        }

    def _default_identifier_map(self) -> Dict[str, tuple]:
        return {
            "s3": ("bucket_name",),
            "ec2": ("instance_id", "name"),
            "rds": ("db_instance_id",),
            "lambda": ("function_name",),
            "vpc": ("vpc_id",),
            "dynamodb": ("table_name",),
            "sns": ("topic_name", "arn"),
            "sqs": ("queue_name", "queue_url"),
            "iam": ("role_name", "user_name", "policy_name", "name"),
            "security_group": ("group_id", "group_name"),
            "ebs": ("volume_id",),
            "cloudwatch": ("alarm_name",),
            "ecs": ("cluster_name",),
            "secretsmanager": ("name", "arn"),
            "kms": ("key_id", "arn", "alias"),
            "ecr": ("repository_name",),
            "efs": ("file_system_id",),
            "acm": ("certificate_arn",),
            "ssm": ("parameter_name",),
            "route53": ("hosted_zone_id", "domain"),
            "apigateway": ("api_id", "api_name"),
            "cloudfront": ("distribution_id", "domain_name"),
            "stepfunctions": ("state_machine_arn", "name"),
            "elasticache": ("cluster_id",),
            "kinesis": ("stream_name",),
            "eks": ("cluster_name",),
            "elb": ("load_balancer_name", "name", "arn"),
            "waf": ("id", "name", "arn"),
            "redshift": ("cluster_id",),
            "emr": ("cluster_id", "cluster_name"),
            "sagemaker": ("notebook_name", "name"),
            "glue": ("crawler_name", "database_name", "name"),
            "athena": ("workgroup_name",),
            "codepipeline": ("pipeline_name",),
            "codebuild": ("project_name",),
            "wellarchitected": ("workload_id", "workload_name"),
            "subnet": ("subnet_id",),
            "nat_gateway": ("nat_gateway_id",),
            "internet_gateway": ("internet_gateway_id",),
            "eip": ("allocation_id", "public_ip"),
            "log_group": ("log_group_name",),
        }
