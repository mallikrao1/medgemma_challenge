"""
AWS Executor Service - Dynamic executor for ALL AWS services.
Supports create, update, delete, list, describe operations.
Uses EXACT user-specified resource names - no default names.
"""

import boto3
import json
from typing import Dict, Any, Optional, List
import uuid
from datetime import datetime
import ast
import os
import re
import asyncio
import base64
import tempfile
import urllib.request
from urllib.parse import unquote
from botocore.signers import RequestSigner
from services.prerequisite_schema import PrerequisiteSchemaService


class AWSExecutor:
    def __init__(self):
        self.session = None
        self.region = "us-west-2"
        self.initialized = False
        self._clients = {}
        self.access_key = None
        self.secret_key = None
        self._schema_helper = PrerequisiteSchemaService()

    def initialize(self, aws_access_key: str, aws_secret_key: str, region: str = "us-west-2"):
        """Initialize AWS session with credentials."""
        try:
            region = region or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
            self.access_key = aws_access_key
            self.secret_key = aws_secret_key
            self.session = boto3.Session(
                aws_access_key_id=aws_access_key,
                aws_secret_access_key=aws_secret_key,
                region_name=region,
            )
            self.region = region
            self._clients = {}
            self.initialized = True
            print(f"  AWS session initialized for region: {region}")
            return True
        except Exception as e:
            print(f"  Failed to initialize AWS: {e}")
            return False

    def _client(self, service: str):
        """Get or create a boto3 client for the given service."""
        if service not in self._clients:
            self._clients[service] = self.session.client(service, region_name=self.region)
        return self._clients[service]

    def _resolve_default_vpc_id(self, ec2_client) -> Optional[str]:
        """Resolve the AWS account default VPC ID in the active region."""
        try:
            response = ec2_client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
            vpcs = response.get("Vpcs", [])
            if vpcs:
                return vpcs[0].get("VpcId")
        except Exception:
            return None
        return None

    def _sanitize_security_group_description(self, description: str) -> str:
        """
        AWS SG description allows a limited character set and max length 255.
        Strip unsupported characters (like apostrophes) before CreateSecurityGroup.
        """
        allowed = re.sub(r"[^a-zA-Z0-9. _:/()#,@\[\]+=&;{}!$*\-]", "", description or "")
        cleaned = allowed.strip()[:255]
        return cleaned or "Security group created by AI Platform"

    def _sanitize_user_data_script(self, script: str) -> str:
        """Normalize cloud-init script and remove noisy placeholder lines."""
        if not isinstance(script, str):
            return ""
        lines = script.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        filtered = []
        for line in lines:
            marker = line.strip().lower()
            if marker in {"default", "none", "null", "n/a", "na"}:
                continue
            filtered.append(line)
        cleaned = "\n".join(filtered).strip()
        if cleaned and not cleaned.startswith("#!/bin/bash"):
            cleaned = "#!/bin/bash\n" + cleaned
        return cleaned

    def _resolve_ami_query(self, os_flavor: Optional[str]) -> Dict[str, Any]:
        flavor = (os_flavor or "amazon-linux-2").strip().lower()
        if flavor == "amazon-linux-2023":
            return {
                "Owners": ["amazon"],
                "Filters": [
                    {"Name": "name", "Values": ["al2023-ami-2023*-x86_64"]},
                    {"Name": "state", "Values": ["available"]},
                ],
            }
        if flavor == "ubuntu-22.04":
            return {
                "Owners": ["099720109477"],
                "Filters": [
                    {"Name": "name", "Values": ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]},
                    {"Name": "state", "Values": ["available"]},
                ],
            }
        if flavor == "ubuntu-20.04":
            return {
                "Owners": ["099720109477"],
                "Filters": [
                    {"Name": "name", "Values": ["ubuntu/images/hvm-ssd/ubuntu-focal-20.04-amd64-server-*"]},
                    {"Name": "state", "Values": ["available"]},
                ],
            }
        if flavor == "windows-2022":
            return {
                "Owners": ["amazon"],
                "Filters": [
                    {"Name": "name", "Values": ["Windows_Server-2022-English-Full-Base-*"]},
                    {"Name": "state", "Values": ["available"]},
                ],
            }
        return {
            "Owners": ["amazon"],
            "Filters": [
                {"Name": "name", "Values": ["amzn2-ami-hvm-*-x86_64-gp2"]},
                {"Name": "state", "Values": ["available"]},
            ],
        }

    def _to_list(self, value: Any) -> list:
        """Normalize list-like values from either list or comma-separated string."""
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return [str(value).strip()] if str(value).strip() else []

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

    def _normalize_wa_environment(self, value: Any) -> str:
        raw = str(value or "").strip()
        upper = raw.upper()
        if upper in {"PRODUCTION", "PREPRODUCTION"}:
            return upper
        text = raw.lower()
        if text in {"prod", "production"}:
            return "PRODUCTION"
        if text in {"preproduction", "pre-prod", "staging", "stage", "qa", "test", "dev", "development"}:
            return "PREPRODUCTION"
        return "PREPRODUCTION"

    def _list_wellarchitected_workloads(self, wa_client) -> list:
        workloads = []
        next_token = None
        while True:
            args = {}
            if next_token:
                args["NextToken"] = next_token
            response = wa_client.list_workloads(**args)
            workloads.extend(response.get("WorkloadSummaries", []))
            next_token = response.get("NextToken")
            if not next_token:
                break
        return workloads

    def _resolve_wellarchitected_workload_id(self, wa_client, reference: Optional[str]) -> Optional[str]:
        ref = (reference or "").strip()
        if not ref:
            return None

        if ref.startswith("wl-"):
            return ref

        try:
            workloads = self._list_wellarchitected_workloads(wa_client)
        except Exception:
            return None

        exact = next((w for w in workloads if w.get("WorkloadName") == ref), None)
        if exact:
            return exact.get("WorkloadId")

        lowered = ref.lower()
        close = next((w for w in workloads if str(w.get("WorkloadName", "")).lower() == lowered), None)
        if close:
            return close.get("WorkloadId")

        return None

    async def execute(
        self,
        action: str,
        resource_type: str,
        resource_name: Optional[str] = None,
        parameters: Dict[str, Any] = None,
        tags: Dict[str, str] = None,
    ) -> Dict[str, Any]:
        """Main dispatch - route action+resource to the correct handler."""
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized. Provide valid credentials."}

        parameters = parameters or {}
        tags = tags or {}

        # Build handler method name
        handler_name = f"_{action}_{resource_type}"
        handler = getattr(self, handler_name, None)

        if handler:
            try:
                return await handler(resource_name=resource_name, parameters=parameters, tags=tags)
            except Exception as e:
                return {"success": False, "error": str(e), "action": action, "resource_type": resource_type}
        else:
            return {
                "success": False,
                "error": f"Operation '{action}' on '{resource_type}' is not yet supported via static handlers. "
                         f"Please use the dynamic Boto3 path for faster, more flexible execution.",
                "action": action,
                "resource_type": resource_type,
            }

    async def list_resource_choices(self, resource_type: str, limit: int = 25) -> Dict[str, Any]:
        """Return normalized existing resource choices for conversational selection."""
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized. Provide valid credentials."}
        resource_type = (resource_type or "").strip().lower()
        if not resource_type:
            return {"success": False, "error": "resource_type is required"}

        alias_map = {
            "spark": "emr",
            "database": "rds",
            "log_group": "log_group",
        }
        target = alias_map.get(resource_type, resource_type)

        result = await self.execute(action="list", resource_type=target, parameters={}, tags={})
        if not result.get("success"):
            return result

        options = self._extract_choice_values(result, limit=limit)
        return {
            "success": True,
            "resource_type": target,
            "options": options,
            "raw": result,
        }

    async def discover_account_resources(
        self,
        resource_types: Optional[List[str]] = None,
        per_type_limit: int = 10,
        include_empty: bool = False,
    ) -> Dict[str, Any]:
        """Discover available resources across services in the current account/region."""
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized. Provide valid credentials."}

        defaults = [
            "ec2", "vpc", "subnet", "security_group", "rds", "s3", "lambda", "eks", "ecs",
            "dynamodb", "sns", "sqs", "iam", "ecr", "efs", "ebs", "cloudwatch",
            "apigateway", "route53", "cloudfront", "ssm", "secretsmanager", "kms",
            "elasticache", "kinesis", "stepfunctions", "emr", "redshift", "sagemaker",
            "glue", "athena", "codepipeline", "codebuild", "wellarchitected",
        ]
        requested = resource_types or defaults
        deduped = []
        seen = set()
        for item in requested:
            normalized = str(item or "").strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)

        inventory: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        limit = max(1, min(int(per_type_limit or 10), 50))
        for resource_type in deduped:
            try:
                listed = await self.list_resource_choices(resource_type=resource_type, limit=limit)
                if not listed.get("success"):
                    errors[resource_type] = str(listed.get("error") or "list failed")
                    continue
                options = listed.get("options", []) if isinstance(listed.get("options"), list) else []
                count = self._estimate_inventory_count(listed.get("raw"), options)
                if not include_empty and count <= 0:
                    continue
                inventory[resource_type] = {
                    "count": count,
                    "sample_ids": options[:limit],
                }
            except Exception as e:
                errors[resource_type] = str(e)

        return {
            "success": True,
            "region": self.region,
            "inventory": inventory,
            "resource_types_scanned": deduped,
            "scanned_count": len(deduped),
            "discovered_count": len(inventory),
            "errors": errors,
            "discovered_at": datetime.utcnow().isoformat(),
        }

    def _estimate_inventory_count(self, raw_result: Any, options: List[str]) -> int:
        if isinstance(raw_result, dict):
            direct = raw_result.get("count")
            if isinstance(direct, int):
                return max(direct, 0)
            for value in raw_result.values():
                if isinstance(value, list):
                    return len(value)
        return len(options or [])

    def _to_python_method_name(self, aws_operation_name: str) -> str:
        if not aws_operation_name:
            return ""
        snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(aws_operation_name)).lower()
        return snake

    def _pick_param_value(self, field_name: str, params: Dict[str, Any], field_to_variable: Dict[str, str]) -> Any:
        if field_name in params and params.get(field_name) not in (None, "", []):
            return params.get(field_name)
        snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(field_name)).lower()
        if snake in params and params.get(snake) not in (None, "", []):
            return params.get(snake)
        mapped = field_to_variable.get(field_name)
        if mapped and mapped in params and params.get(mapped) not in (None, "", []):
            return params.get(mapped)
        return None

    def _coerce_shape_value(self, value: Any, shape: Any) -> Any:
        if value is None:
            return value
        type_name = str(getattr(shape, "type_name", "string"))
        if type_name in {"integer", "long"}:
            try:
                return int(value)
            except Exception:
                return value
        if type_name in {"float", "double"}:
            try:
                return float(value)
            except Exception:
                return value
        if type_name == "boolean":
            return self._to_bool(value, default=False)
        if type_name == "list":
            if isinstance(value, str):
                text = value.strip()
                if text.startswith("[") or text.startswith("{"):
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, list):
                            return parsed
                        if isinstance(parsed, dict):
                            return [parsed]
                    except Exception:
                        pass
            return self._to_list(value)
        if type_name == "structure" and isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return value
        return value

    def _name_field_for_resource(self, resource_type: str, operation_name: str) -> Optional[str]:
        by_resource = {
            "s3": "Bucket",
            "dynamodb": "TableName",
            "sns": "Name",
            "sqs": "QueueName",
            "lambda": "FunctionName",
            "rds": "DBInstanceIdentifier",
            "eks": "name",
            "ecs": "clusterName",
            "ecr": "repositoryName",
            "vpc": "VpcId",
            "subnet": "SubnetId",
            "security_group": "GroupName",
            "wellarchitected": "WorkloadName",
        }
        if resource_type in by_resource:
            return by_resource[resource_type]
        lower_op = str(operation_name or "").lower()
        if "cluster" in lower_op:
            return "ClusterName"
        if "function" in lower_op:
            return "FunctionName"
        if "bucket" in lower_op:
            return "Bucket"
        return None

    def _build_model_driven_questions(self, missing_fields: List[str], field_to_variable: Dict[str, str]) -> List[Dict[str, Any]]:
        questions = []
        role_fields = {"role", "rolearn", "executionrolearn", "servicerole"}
        for field_name in missing_fields:
            normalized = str(field_name or "").strip().lower()
            if normalized in role_fields:
                if not any(item.get("variable") == "iam_permissions_ready" for item in questions):
                    questions.append(
                        {
                            "variable": "iam_permissions_ready",
                            "question": "I could not auto-provision the required IAM role. Please grant IAM role-management permissions and retry this run.",
                            "type": "string",
                            "hint": "Required permissions include iam:CreateRole, iam:AttachRolePolicy, iam:PassRole.",
                        }
                    )
                continue
            variable = field_to_variable.get(field_name) or re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", field_name).lower()
            qtype = "string"
            if field_name.lower().endswith("password"):
                qtype = "password"
            questions.append(
                {
                    "variable": variable,
                    "question": f"Please provide value for {field_name}.",
                    "type": qtype,
                }
            )
        return questions

    async def execute_model_driven(
        self,
        action: str,
        resource_type: str,
        resource_name: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Generic model-driven executor using botocore operation metadata."""
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized. Provide valid credentials."}

        action = str(action or "").strip().lower()
        resource_type = str(resource_type or "").strip().lower()
        params = parameters or {}
        tags = tags or {}
        if isinstance(params.get("TagSpecifications"), str):
            params = dict(params)
            params.pop("TagSpecifications", None)
        if action not in {"create", "update", "delete", "list", "describe"} or not resource_type:
            return {"success": False, "error": "Unsupported action/resource for model-driven execution."}

        service_name = self._schema_helper.SERVICE_ALIASES.get(resource_type, resource_type)
        operation_name = self._schema_helper._resolve_operation_name(service_name, resource_type, action)
        if not operation_name:
            return {
                "success": False,
                "error": f"Could not resolve AWS operation for action={action} resource={resource_type}",
            }

        try:
            client = self._client(service_name)
            operation_model = client.meta.service_model.operation_model(operation_name)
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to load operation model {operation_name}: {e}",
            }

        payload: Dict[str, Any] = {}
        missing_fields: List[str] = []
        field_to_variable = dict(self._schema_helper.FIELD_TO_VARIABLE)
        input_shape = operation_model.input_shape
        required_members = list(getattr(input_shape, "required_members", []) or []) if input_shape else []
        name_field = self._name_field_for_resource(resource_type, operation_name)

        if input_shape:
            for field_name, shape in input_shape.members.items():
                value = self._pick_param_value(field_name, params, field_to_variable)
                if isinstance(value, str):
                    lowered_value = value.strip().lower()
                    if lowered_value in {"default", "auto", "automatic", "none", "null"} and field_name in {"SubnetId", "VpcId", "SecurityGroupIds"}:
                        value = None

                if value is None and field_name == name_field and resource_name:
                    value = resource_name
                if value is None and field_name in {"MinCount", "MaxCount"}:
                    value = 1
                if value is None and field_name == "TagSpecifications" and tags and service_name == "ec2":
                    value = [{
                        "ResourceType": "instance",
                        "Tags": [{"Key": str(k), "Value": str(v)} for k, v in tags.items()],
                    }]
                if value is None and field_name == "Tags" and tags:
                    value = [{"Key": str(k), "Value": str(v)} for k, v in tags.items()]
                if value is None and field_name == "ImageId" and service_name == "ec2" and operation_name == "RunInstances":
                    try:
                        ec2 = self._client("ec2")
                        query = self._resolve_ami_query(params.get("os_flavor"))
                        images = ec2.describe_images(**query).get("Images", [])
                        images = sorted(images, key=lambda item: item.get("CreationDate", ""), reverse=True)
                        if images:
                            value = images[0].get("ImageId")
                    except Exception:
                        value = None

                if value is None:
                    if field_name in required_members:
                        missing_fields.append(field_name)
                    continue

                payload[field_name] = self._coerce_shape_value(value, shape)

        # Service-aware auto-fill for required fields (prevents unnecessary follow-up prompts).
        if service_name == "eks" and operation_name == "CreateCluster":
            if "roleArn" in missing_fields:
                role_result = await self.ensure_eks_cluster_role(
                    environment=str(params.get("environment") or "dev"),
                    cluster_name=resource_name,
                    tags=tags,
                )
                if role_result.get("success") and role_result.get("role_arn"):
                    payload["roleArn"] = role_result.get("role_arn")
                    missing_fields = [item for item in missing_fields if item != "roleArn"]

            if "resourcesVpcConfig" in missing_fields:
                subnet_ids = self._to_list(
                    params.get("subnet_ids")
                    or params.get("subnetIds")
                    or params.get("SubnetIds")
                )
                if len(subnet_ids) < 2:
                    subnet_result = await self.discover_eks_subnet_ids(
                        vpc_id=params.get("vpc_id"),
                        min_count=2,
                    )
                    if subnet_result.get("success"):
                        subnet_ids = self._to_list(subnet_result.get("subnet_ids"))

                if len(subnet_ids) >= 2:
                    vpc_cfg = payload.get("resourcesVpcConfig", {})
                    if not isinstance(vpc_cfg, dict):
                        vpc_cfg = {}
                    vpc_cfg["subnetIds"] = subnet_ids[:]
                    sg_ids = self._to_list(
                        params.get("security_group_ids")
                        or params.get("securityGroupIds")
                        or params.get("SecurityGroupIds")
                    )
                    if sg_ids:
                        vpc_cfg["securityGroupIds"] = sg_ids
                    payload["resourcesVpcConfig"] = vpc_cfg
                    missing_fields = [item for item in missing_fields if item != "resourcesVpcConfig"]

        if missing_fields:
            auto_prefill = await self.auto_fill_intent_prerequisites(
                action=action,
                resource_type=resource_type,
                resource_name=resource_name,
                parameters=params,
                tags=tags,
                environment=str(params.get("environment") or "dev"),
            )
            if auto_prefill.get("success"):
                params = auto_prefill.get("parameters", params)
                role_arn = params.get("role_arn")
                role_fields = {"RoleArn", "roleArn", "ExecutionRoleArn", "ServiceRole", "serviceRole", "Role"}
                for field_name in list(missing_fields):
                    lowered = str(field_name).lower()
                    if field_name in role_fields and role_arn:
                        payload[field_name] = role_arn
                        missing_fields.remove(field_name)
                        continue
                    if lowered in {"vpcid"} and params.get("vpc_id"):
                        payload[field_name] = params.get("vpc_id")
                        missing_fields.remove(field_name)
                        continue
                    if lowered in {"subnetid"} and params.get("subnet_id"):
                        payload[field_name] = params.get("subnet_id")
                        missing_fields.remove(field_name)
                        continue
                    if lowered in {"subnetids"}:
                        subnet_ids = self._to_list(params.get("subnet_ids") or params.get("subnetIds"))
                        if subnet_ids:
                            payload[field_name] = subnet_ids
                            missing_fields.remove(field_name)
                            continue
                    if lowered in {"securitygroupids"}:
                        sg_ids = self._to_list(params.get("security_group_ids") or params.get("securityGroupIds"))
                        if sg_ids:
                            payload[field_name] = sg_ids
                            missing_fields.remove(field_name)
                            continue
                    if lowered == "resourcesvpcconfig":
                        subnet_ids = self._to_list(params.get("subnet_ids"))
                        if len(subnet_ids) >= 2:
                            payload[field_name] = {"subnetIds": subnet_ids}
                            sg_ids = self._to_list(params.get("security_group_ids"))
                            if sg_ids:
                                payload[field_name]["securityGroupIds"] = sg_ids
                            missing_fields.remove(field_name)
                            continue
                    if lowered == "mincount":
                        payload[field_name] = int(params.get("MinCount", 1) or 1)
                        missing_fields.remove(field_name)
                        continue
                    if lowered == "maxcount":
                        payload[field_name] = int(params.get("MaxCount", 1) or 1)
                        missing_fields.remove(field_name)
                        continue

        if missing_fields:
            role_fields = {"role", "rolearn", "executionrolearn", "servicerole", "serviceRole".lower()}
            unresolved_role_fields = [
                field_name
                for field_name in missing_fields
                if str(field_name or "").strip().lower() in role_fields
            ]
            if unresolved_role_fields:
                return {
                    "success": False,
                    "requires_input": True,
                    "question_prompt": (
                        "I can auto-create the required IAM service role, but this AWS identity is currently "
                        "blocked from role-management actions."
                    ),
                    "questions": [
                        {
                            "variable": "iam_permissions_ready",
                            "question": "Please allow IAM role-management permissions, then reply done to continue.",
                            "type": "string",
                            "hint": "Required: iam:CreateRole, iam:AttachRolePolicy, iam:PassRole, iam:GetRole.",
                            "options": ["done"],
                        }
                    ],
                    "action": action,
                    "resource_type": resource_type,
                    "operation": operation_name,
                    "required_permissions": [
                        "iam:CreateRole",
                        "iam:AttachRolePolicy",
                        "iam:PassRole",
                        "iam:GetRole",
                    ],
                }
            return {
                "success": False,
                "requires_input": True,
                "question_prompt": "I need a few required values before I can execute this service operation.",
                "questions": self._build_model_driven_questions(missing_fields, field_to_variable),
                "action": action,
                "resource_type": resource_type,
                "operation": operation_name,
            }

        method_name = self._to_python_method_name(operation_name)
        api = getattr(client, method_name, None)
        if not api:
            return {
                "success": False,
                "error": f"Client method not available for operation {operation_name}",
            }

        try:
            response = api(**payload) if payload else api()
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "action": action,
                "resource_type": resource_type,
                "operation": operation_name,
            }

        details: Dict[str, Any] = {
            "operation": operation_name,
            "service": service_name,
        }
        if isinstance(response, dict):
            for key in [
                "InstanceId", "DBInstanceIdentifier", "FunctionArn", "FunctionName", "VpcId", "SubnetId", "GroupId",
                "TableName", "TopicArn", "QueueUrl", "LoadBalancerArn", "ClusterArn", "ClusterName", "WorkloadId",
                "BucketArn", "BucketName",
            ]:
                if key in response:
                    details[re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower()] = response.get(key)
            details["response_keys"] = list(response.keys())[:20]

        result = {
            "success": True,
            "action": action,
            "resource_type": resource_type,
            "resource_name": resource_name,
            "execution_path": "model_driven_fallback",
            "details": details,
        }
        if tags:
            result["tags_applied"] = True
        return result

    def _extract_choice_values(self, payload: Dict[str, Any], limit: int = 25) -> list:
        choices = []
        candidate_keys = [
            "instance_id",
            "db_instance_id",
            "cluster_id",
            "cluster_name",
            "function_name",
            "table_name",
            "bucket_name",
            "queue_url",
            "queue_name",
            "topic_arn",
            "stream_name",
            "repository_name",
            "log_group_name",
            "workload_id",
            "workload_name",
            "name",
            "id",
            "arn",
        ]

        def collect(node):
            if len(choices) >= limit:
                return
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, list):
                        for item in value:
                            collect(item)
                    elif isinstance(value, dict):
                        collect(value)
                return
            if isinstance(node, str):
                cleaned = node.strip()
                if cleaned and cleaned not in choices:
                    choices.append(cleaned)
                return
            if isinstance(node, list):
                for item in node:
                    collect(item)
                return
            if not isinstance(node, dict):
                return

        # Direct extraction from list entries first.
        for value in payload.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if len(choices) >= limit:
                    break
                if isinstance(item, str):
                    cleaned = item.strip()
                    if cleaned and cleaned not in choices:
                        choices.append(cleaned)
                    continue
                if not isinstance(item, dict):
                    continue
                selected = None
                for k in candidate_keys:
                    v = item.get(k)
                    if isinstance(v, str) and v.strip():
                        selected = v.strip()
                        break
                if selected and selected not in choices:
                    choices.append(selected)

        # Fallback recursive extraction.
        if not choices:
            collect(payload)

        return choices[:limit]

    async def check_ec2_instance_readiness(self, instance_id: str) -> Dict[str, Any]:
        """Check EC2 state + status checks for a given instance ID."""
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not instance_id:
            return {"success": False, "error": "instance_id is required"}

        ec2 = self._client("ec2")
        desc = ec2.describe_instances(InstanceIds=[instance_id])
        reservations = desc.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            return {"success": False, "error": f"Instance {instance_id} not found"}

        inst = reservations[0]["Instances"][0]
        state = inst.get("State", {}).get("Name", "unknown")
        public_ip = inst.get("PublicIpAddress")
        private_ip = inst.get("PrivateIpAddress")

        status_resp = ec2.describe_instance_status(
            InstanceIds=[instance_id],
            IncludeAllInstances=True,
        )
        statuses = status_resp.get("InstanceStatuses", [])
        instance_status = "initializing"
        system_status = "initializing"
        if statuses:
            instance_status = statuses[0].get("InstanceStatus", {}).get("Status", "initializing")
            system_status = statuses[0].get("SystemStatus", {}).get("Status", "initializing")

        ready = state == "running" and instance_status == "ok" and system_status == "ok"
        return {
            "success": True,
            "instance_id": instance_id,
            "state": state,
            "instance_status": instance_status,
            "system_status": system_status,
            "ready": ready,
            "public_ip": public_ip,
            "private_ip": private_ip,
            "region": self.region,
        }

    def _build_app_install_commands(self, app_targets: Any, custom_commands: Optional[Any] = None) -> list:
        targets = []
        if isinstance(app_targets, str):
            targets = [x.strip().lower() for x in app_targets.split(",") if x.strip()]
        elif isinstance(app_targets, list):
            targets = [str(x).strip().lower() for x in app_targets if str(x).strip()]

        commands = [
            "set -euxo pipefail",
            "if command -v dnf >/dev/null 2>&1; then PM=dnf; else PM=yum; fi",
            "$PM update -y",
        ]

        for target in targets:
            if target == "tomcat":
                commands.extend([
                    "$PM install -y java-17-amazon-corretto",
                    "TOMCAT_VERSION=10.1.28",
                    "cd /opt",
                    "curl -fsSL -o apache-tomcat.tar.gz https://dlcdn.apache.org/tomcat/tomcat-10/v${TOMCAT_VERSION}/bin/apache-tomcat-${TOMCAT_VERSION}.tar.gz",
                    "tar -xzf apache-tomcat.tar.gz",
                    "ln -sfn apache-tomcat-${TOMCAT_VERSION} tomcat",
                    "chmod +x /opt/tomcat/bin/*.sh",
                    "nohup /opt/tomcat/bin/startup.sh >/var/log/tomcat-start.log 2>&1 &",
                ])
            elif target == "nginx":
                commands.extend(["$PM install -y nginx", "systemctl enable nginx", "systemctl start nginx"])
            elif target == "docker":
                commands.extend(["$PM install -y docker", "systemctl enable docker", "systemctl start docker"])
            elif target == "node":
                commands.extend(["$PM install -y nodejs npm"])
            elif target == "python":
                commands.extend(["$PM install -y python3 python3-pip"])
            elif target == "java":
                commands.extend(["$PM install -y java-17-amazon-corretto"])
            elif target == "git":
                commands.extend(["$PM install -y git"])
            elif target:
                commands.extend([f"echo 'Custom target requested: {target}'"])

        if isinstance(custom_commands, str) and custom_commands.strip():
            commands.extend([line for line in custom_commands.split("\n") if line.strip()])
        elif isinstance(custom_commands, list):
            commands.extend([str(line) for line in custom_commands if str(line).strip()])

        return commands

    async def _open_instance_port_if_needed(self, instance_id: str, app_port: Optional[int], public_access: bool):
        if not app_port or not public_access:
            return
        ec2 = self._client("ec2")
        desc = ec2.describe_instances(InstanceIds=[instance_id])
        reservations = desc.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            return
        inst = reservations[0]["Instances"][0]
        for sg in inst.get("SecurityGroups", []):
            sg_id = sg.get("GroupId")
            if not sg_id:
                continue
            try:
                ec2.authorize_security_group_ingress(
                    GroupId=sg_id,
                    IpPermissions=[{
                        "IpProtocol": "tcp",
                        "FromPort": int(app_port),
                        "ToPort": int(app_port),
                        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                    }],
                )
            except Exception as e:
                if "InvalidPermission.Duplicate" in str(e):
                    continue
                raise

    async def deploy_to_ec2_via_ssm(
        self,
        instance_id: str,
        app_targets: Any,
        app_port: Optional[int] = None,
        public_access: bool = False,
        custom_commands: Optional[Any] = None,
        wait_seconds: int = 300,
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not instance_id:
            return {"success": False, "error": "instance_id is required"}

        waited = 0
        readiness = await self.check_ec2_instance_readiness(instance_id)
        while readiness.get("success") and not readiness.get("ready") and waited < max(wait_seconds, 0):
            await asyncio.sleep(15)
            waited += 15
            readiness = await self.check_ec2_instance_readiness(instance_id)

        if not readiness.get("success"):
            return readiness
        if not readiness.get("ready"):
            return {
                "success": False,
                "error": "Instance is not ready for SSM deployment yet.",
                "requires_wait": True,
                "waited_seconds": waited,
                "readiness": readiness,
            }

        ssm = self._client("ssm")
        info = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        if not info.get("InstanceInformationList"):
            return {
                "success": False,
                "error": "Instance is not managed by SSM. Attach IAM role with AmazonSSMManagedInstanceCore and ensure SSM agent is running.",
                "requires_input": True,
                "remediation_hint": "ssm_prereq_missing",
                "next_retry_seconds": 30,
                "questions": [
                    {
                        "variable": "enable_ssm_role",
                        "question": "SSM role missing. Attach AmazonSSMManagedInstanceCore role and reply 'done' to continue.",
                        "type": "string",
                    }
                ],
            }

        commands = self._build_app_install_commands(app_targets, custom_commands=custom_commands)
        send = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": commands},
            Comment="AI Infra Agent auto deployment",
            TimeoutSeconds=3600,
        )
        command_id = send["Command"]["CommandId"]

        final = None
        for _ in range(120):
            await asyncio.sleep(5)
            try:
                inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            except Exception:
                continue
            final = inv
            if inv.get("Status") in {"Success", "Failed", "TimedOut", "Cancelled", "Undeliverable", "Terminated"}:
                break

        if app_port:
            await self._open_instance_port_if_needed(instance_id, int(app_port), bool(public_access))

        status = (final or {}).get("Status", "Unknown")
        stdout = (final or {}).get("StandardOutputContent", "")
        stderr = (final or {}).get("StandardErrorContent", "")

        return {
            "success": status == "Success",
            "action": "deploy",
            "resource_type": "ec2",
            "instance_id": instance_id,
            "command_id": command_id,
            "ssm_status": status,
            "app_targets": app_targets,
            "app_port": app_port,
            "public_access": public_access,
            "stdout_tail": stdout[-3000:],
            "stderr_tail": stderr[-3000:],
            "readiness": readiness,
            "region": self.region,
        }

    async def execute_rds_sql_via_ssm(
        self,
        rds_instance_id: str,
        sql: str,
        db_name: Optional[str] = None,
        db_username: Optional[str] = None,
        db_password: Optional[str] = None,
        secret_arn: Optional[str] = None,
        bastion_instance_id: Optional[str] = None,
        ensure_network_path: bool = True,
        wait_seconds: int = 300,
    ) -> Dict[str, Any]:
        """
        Execute SQL on RDS using SSM RunCommand via a bastion/worker EC2 instance.
        """
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not rds_instance_id:
            return {"success": False, "error": "rds_instance_id is required"}
        sql_text = (sql or "").strip()
        if not sql_text:
            return {"success": False, "error": "sql statement is required"}

        rds = self._client("rds")
        ssm = self._client("ssm")

        db_instance = self._resolve_rds_instance(rds, rds_instance_id)
        if not db_instance:
            return {"success": False, "error": f"RDS instance '{rds_instance_id}' not found"}

        db_identifier = db_instance.get("DBInstanceIdentifier")
        engine = str(db_instance.get("Engine", "")).lower()
        endpoint = db_instance.get("Endpoint", {}) or {}
        db_host = endpoint.get("Address")
        db_port = int(endpoint.get("Port") or (5432 if "postgres" in engine else 3306))
        selected_db_name = db_name or db_instance.get("DBName") or "postgres"

        resolved_user = db_username
        resolved_password = db_password
        if secret_arn and (not resolved_user or not resolved_password):
            secret_val = self._resolve_db_secret(secret_arn)
            resolved_user = resolved_user or secret_val.get("username")
            resolved_password = resolved_password or secret_val.get("password")
            selected_db_name = db_name or secret_val.get("dbname") or selected_db_name

        if not resolved_user or not resolved_password:
            return {
                "success": False,
                "requires_input": True,
                "question_prompt": "I need database credentials to run SQL on RDS.",
                "questions": [
                    {"variable": "db_username", "question": "Provide DB username.", "type": "string"},
                    {"variable": "db_password", "question": "Provide DB password.", "type": "password"},
                    {"variable": "secret_arn", "question": "Or provide Secrets Manager ARN containing username/password (optional).", "type": "string"},
                ],
            }

        selected_bastion = bastion_instance_id
        if not selected_bastion:
            selected_bastion = await self._discover_bastion_instance_id()
            if not selected_bastion:
                return {
                    "success": False,
                    "requires_input": True,
                    "question_prompt": "I need an SSM-managed EC2 bastion/worker instance to reach RDS.",
                    "questions": [
                        {
                            "variable": "bastion_instance_id",
                            "question": "Provide EC2 instance ID for bastion/worker (SSM-managed).",
                            "type": "string",
                            "hint": "Example: i-0123456789abcdef0",
                        }
                    ],
                }

        bastion_ready = await self.check_ec2_instance_readiness(selected_bastion)
        waited = 0
        while bastion_ready.get("success") and not bastion_ready.get("ready") and waited < max(wait_seconds, 0):
            await asyncio.sleep(15)
            waited += 15
            bastion_ready = await self.check_ec2_instance_readiness(selected_bastion)
        if not bastion_ready.get("success"):
            return {"success": False, "error": bastion_ready.get("error")}
        if not bastion_ready.get("ready"):
            return {
                "success": False,
                "requires_wait": True,
                "waited_seconds": waited,
                "error": f"Bastion instance {selected_bastion} is not ready yet.",
            }

        if not await self._wait_for_ssm_managed(ssm, selected_bastion, wait_seconds=max(wait_seconds, 0)):
            return {
                "success": False,
                "error": "Bastion instance is not SSM managed. Attach AmazonSSMManagedInstanceCore role and ensure SSM agent is running.",
                "remediation_hint": "ssm_prereq_missing",
                "next_retry_seconds": 30,
            }

        if ensure_network_path:
            try:
                await self._authorize_rds_from_bastion(selected_bastion, db_instance, db_port)
            except Exception as e:
                return {"success": False, "error": f"Failed to authorize bastion->RDS network path: {e}"}

        engine_mode = self._normalize_sql_engine_mode(engine)
        if not engine_mode:
            return {
                "success": False,
                "error": f"Engine '{engine}' not supported by SQL-over-SSM executor (supported: postgres/mysql/mariadb/aurora).",
            }

        sql_b64 = base64.b64encode(sql_text.encode("utf-8")).decode("ascii")
        pass_b64 = base64.b64encode(str(resolved_password).encode("utf-8")).decode("ascii")

        commands = [
            "set -euxo pipefail",
            "if command -v dnf >/dev/null 2>&1; then PM=dnf; else PM=yum; fi",
            "$PM update -y",
        ]
        if engine_mode == "postgres":
            commands.append("$PM install -y postgresql15 || $PM install -y postgresql || true")
        else:
            commands.append("$PM install -y mariadb105 || $PM install -y mariadb || $PM install -y mysql || true")

        commands.extend([
            f'DB_HOST="{db_host}"',
            f'DB_PORT="{db_port}"',
            f'DB_NAME="{selected_db_name}"',
            f'DB_USER="{resolved_user}"',
            f'DB_PASSWORD_B64="{pass_b64}"',
            f'SQL_QUERY_B64="{sql_b64}"',
            'DB_PASSWORD="$(echo "$DB_PASSWORD_B64" | base64 --decode)"',
            'SQL_QUERY="$(echo "$SQL_QUERY_B64" | base64 --decode)"',
        ])

        if engine_mode == "postgres":
            commands.append('PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "$SQL_QUERY"')
        else:
            commands.append('mysql --host="$DB_HOST" --port="$DB_PORT" --user="$DB_USER" --password="$DB_PASSWORD" "$DB_NAME" -e "$SQL_QUERY"')

        send = ssm.send_command(
            InstanceIds=[selected_bastion],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": commands},
            Comment=f"AI Infra Agent RDS SQL execution for {db_identifier}",
            TimeoutSeconds=3600,
        )
        command_id = send["Command"]["CommandId"]

        final = None
        for _ in range(120):
            await asyncio.sleep(5)
            try:
                invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=selected_bastion)
            except Exception:
                continue
            final = invocation
            if invocation.get("Status") in {"Success", "Failed", "TimedOut", "Cancelled", "Undeliverable", "Terminated"}:
                break

        status = (final or {}).get("Status", "Unknown")
        stdout = (final or {}).get("StandardOutputContent", "")
        stderr = (final or {}).get("StandardErrorContent", "")
        return {
            "success": status == "Success",
            "action": "update",
            "resource_type": "rds",
            "execution_mode": "ssm_bastion_sql",
            "db_instance_id": db_identifier,
            "engine": engine,
            "db_name": selected_db_name,
            "db_host": db_host,
            "db_port": db_port,
            "bastion_instance_id": selected_bastion,
            "command_id": command_id,
            "ssm_status": status,
            "stdout_tail": stdout[-3000:],
            "stderr_tail": stderr[-3000:],
            "region": self.region,
        }

    def _resolve_rds_instance(self, rds_client, instance_ref: str) -> Optional[Dict[str, Any]]:
        ref = (instance_ref or "").strip()
        if not ref:
            return None
        try:
            response = rds_client.describe_db_instances(DBInstanceIdentifier=ref)
            dbs = response.get("DBInstances", [])
            if dbs:
                return dbs[0]
        except Exception:
            pass

        try:
            paginator = rds_client.get_paginator("describe_db_instances")
            for page in paginator.paginate():
                for db in page.get("DBInstances", []):
                    identifier = db.get("DBInstanceIdentifier", "")
                    endpoint = (db.get("Endpoint", {}) or {}).get("Address", "")
                    if ref == identifier or ref.lower() == identifier.lower() or ref == endpoint:
                        return db
        except Exception:
            return None
        return None

    def _resolve_db_secret(self, secret_arn: str) -> Dict[str, Any]:
        secrets = self._client("secretsmanager")
        response = secrets.get_secret_value(SecretId=secret_arn)
        secret_string = response.get("SecretString")
        if not secret_string:
            return {}
        try:
            parsed = json.loads(secret_string)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
        return {}

    async def _discover_bastion_instance_id(self) -> Optional[str]:
        ec2 = self._client("ec2")
        candidates = []
        filters = [{"Name": "instance-state-name", "Values": ["running"]}]
        for name_filter in ["*bastion*", "*jump*", "*admin*"]:
            try:
                response = ec2.describe_instances(
                    Filters=filters + [{"Name": "tag:Name", "Values": [name_filter]}]
                )
                for reservation in response.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        instance_id = instance.get("InstanceId")
                        if instance_id and instance_id not in candidates:
                            candidates.append(instance_id)
            except Exception:
                continue

        if not candidates:
            try:
                response = ec2.describe_instances(Filters=filters)
                for reservation in response.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        tags = {t.get("Key", ""): t.get("Value", "") for t in instance.get("Tags", [])}
                        role = str(tags.get("Role", "")).lower()
                        if role in {"bastion", "jump", "admin"}:
                            instance_id = instance.get("InstanceId")
                            if instance_id and instance_id not in candidates:
                                candidates.append(instance_id)
            except Exception:
                return None

        if not candidates:
            return None

        ssm = self._client("ssm")
        try:
            info = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": candidates[:50]}]
            )
            managed_ids = {item.get("InstanceId") for item in info.get("InstanceInformationList", [])}
            for instance_id in candidates:
                if instance_id in managed_ids:
                    return instance_id
        except Exception:
            return None
        return None

    async def _wait_for_ssm_managed(self, ssm_client, instance_id: str, wait_seconds: int = 300) -> bool:
        waited = 0
        while waited <= max(wait_seconds, 0):
            try:
                info = ssm_client.describe_instance_information(
                    Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
                )
                if info.get("InstanceInformationList"):
                    return True
            except Exception:
                pass
            if waited >= max(wait_seconds, 0):
                break
            await asyncio.sleep(15)
            waited += 15
        return False

    async def is_instance_ssm_managed(self, instance_id: str) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not instance_id:
            return {"success": False, "error": "instance_id is required"}
        ssm = self._client("ssm")
        try:
            info = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
            )
            items = info.get("InstanceInformationList", [])
            if not items:
                return {"success": True, "managed": False, "instance_id": instance_id}
            item = items[0]
            return {
                "success": True,
                "managed": True,
                "instance_id": instance_id,
                "ping_status": item.get("PingStatus"),
                "platform_type": item.get("PlatformType"),
                "last_ping": str(item.get("LastPingDateTime") or ""),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def wait_for_ssm_registration(self, instance_id: str, timeout_seconds: int = 300) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        ssm = self._client("ssm")
        managed = await self._wait_for_ssm_managed(ssm, instance_id, wait_seconds=max(timeout_seconds, 0))
        return {
            "success": bool(managed),
            "instance_id": instance_id,
            "managed": bool(managed),
            "timeout_seconds": int(timeout_seconds),
            "error": None if managed else "Instance did not register as SSM managed within timeout.",
        }

    async def ensure_iam_policy_attached(self, role_name: str, policy_arn: str) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not role_name or not policy_arn:
            return {"success": False, "error": "role_name and policy_arn are required"}
        iam = self._client("iam")
        try:
            paginator = iam.get_paginator("list_attached_role_policies")
            for page in paginator.paginate(RoleName=role_name):
                for item in page.get("AttachedPolicies", []):
                    if item.get("PolicyArn") == policy_arn:
                        return {
                            "success": True,
                            "role_name": role_name,
                            "policy_arn": policy_arn,
                            "attached": True,
                            "changed": False,
                        }
            iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
            return {
                "success": True,
                "role_name": role_name,
                "policy_arn": policy_arn,
                "attached": True,
                "changed": True,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _role_trusts_service_principal(self, assume_policy_doc: Any, service_principal: str) -> bool:
        principal = str(service_principal or "").strip().lower()
        if not principal:
            return False

        doc = assume_policy_doc
        if isinstance(doc, str):
            raw = doc.strip()
            try:
                doc = json.loads(raw)
            except Exception:
                try:
                    doc = json.loads(unquote(raw))
                except Exception:
                    return False
        if not isinstance(doc, dict):
            return False

        statements = doc.get("Statement", [])
        if isinstance(statements, dict):
            statements = [statements]
        if not isinstance(statements, list):
            return False

        for statement in statements:
            if not isinstance(statement, dict):
                continue
            principal_block = statement.get("Principal", {})
            if not isinstance(principal_block, dict):
                continue
            service_value = principal_block.get("Service")
            if isinstance(service_value, str):
                values = [service_value]
            elif isinstance(service_value, list):
                values = [str(item) for item in service_value]
            else:
                values = []
            if any(str(item).strip().lower() == principal for item in values):
                return True
        return False

    async def discover_existing_service_role(
        self,
        service_principal: str,
        role_name_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}

        iam = self._client("iam")
        hint = str(role_name_hint or "").strip()
        if hint:
            try:
                resp = iam.get_role(RoleName=hint)
                role = resp.get("Role", {})
                if self._role_trusts_service_principal(role.get("AssumeRolePolicyDocument"), service_principal):
                    return {
                        "success": True,
                        "role_name": role.get("RoleName"),
                        "role_arn": role.get("Arn"),
                        "source": "hint",
                    }
            except Exception:
                pass

        scanned = 0
        try:
            paginator = iam.get_paginator("list_roles")
            for page in paginator.paginate(PaginationConfig={"MaxItems": 300, "PageSize": 100}):
                for role in page.get("Roles", []):
                    scanned += 1
                    if scanned > 300:
                        break
                    if self._role_trusts_service_principal(role.get("AssumeRolePolicyDocument"), service_principal):
                        return {
                            "success": True,
                            "role_name": role.get("RoleName"),
                            "role_arn": role.get("Arn"),
                            "source": "inventory",
                        }
                if scanned > 300:
                    break
        except Exception as e:
            return {"success": False, "error": str(e)}

        return {"success": False, "error": f"No existing role found for service principal {service_principal}."}

    async def ensure_managed_instance_profile(self, environment: str, role_model: str = "shared") -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        env_slug = re.sub(r"[^a-zA-Z0-9-]", "-", str(environment or "dev").lower()).strip("-") or "dev"
        model = str(role_model or "shared").lower()
        suffix = env_slug if model == "shared" else f"{env_slug}-{uuid.uuid4().hex[:6]}"
        role_name = f"AIAgent-SSMManagedRole-{suffix}"
        profile_name = f"AIAgent-SSMManagedProfile-{suffix}"
        iam = self._client("iam")

        trust_policy = json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        })

        role_created = False
        try:
            iam.get_role(RoleName=role_name)
        except Exception:
            iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=trust_policy,
                Description="Managed by AI Infra Platform for SSM automation.",
            )
            role_created = True

        policy_result = await self.ensure_iam_policy_attached(
            role_name=role_name,
            policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        )
        if not policy_result.get("success"):
            return policy_result

        profile_created = False
        try:
            iam.get_instance_profile(InstanceProfileName=profile_name)
        except Exception:
            iam.create_instance_profile(
                InstanceProfileName=profile_name,
            )
            profile_created = True

        role_in_profile = False
        try:
            profile = iam.get_instance_profile(InstanceProfileName=profile_name).get("InstanceProfile", {})
            roles = profile.get("Roles", [])
            role_in_profile = any(str(r.get("RoleName")) == role_name for r in roles)
        except Exception:
            role_in_profile = False

        if not role_in_profile:
            try:
                iam.add_role_to_instance_profile(
                    InstanceProfileName=profile_name,
                    RoleName=role_name,
                )
                await asyncio.sleep(2)
            except Exception as e:
                if "LimitExceeded" not in str(e) and "EntityAlreadyExists" not in str(e):
                    return {"success": False, "error": str(e)}

        return {
            "success": True,
            "role_name": role_name,
            "profile_name": profile_name,
            "role_created": role_created,
            "profile_created": profile_created,
            "policy_attached": True,
        }

    async def ensure_eks_cluster_role(
        self,
        environment: str = "dev",
        cluster_name: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        env_slug = re.sub(r"[^a-zA-Z0-9-]", "-", str(environment or "dev").lower()).strip("-") or "dev"
        explicit = str(cluster_name or "").strip()
        role_name = f"{explicit}-eks-role" if explicit else f"AIAgent-EKSClusterRole-{env_slug}"
        return await self.ensure_service_role(
            service_slug="EKSCluster",
            service_principal="eks.amazonaws.com",
            policy_arns=[
                "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController",
            ],
            environment=environment,
            role_name=role_name[:64],
            tags=tags,
        )

    async def discover_eks_subnet_ids(
        self,
        vpc_id: Optional[str] = None,
        min_count: int = 2,
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}

        ec2 = self._client("ec2")
        target_vpc = str(vpc_id or "").strip()
        if not target_vpc:
            target_vpc = self._resolve_default_vpc_id(ec2)
        if not target_vpc:
            return {"success": False, "error": "No default VPC found. Provide vpc_id/subnet_ids."}

        filters = [
            {"Name": "vpc-id", "Values": [target_vpc]},
            {"Name": "state", "Values": ["available"]},
        ]
        subnets = ec2.describe_subnets(Filters=filters).get("Subnets", [])
        if not subnets:
            return {"success": False, "error": f"No available subnets found in VPC {target_vpc}."}

        # Prefer one subnet per AZ first, then fill remainder.
        seen_az = set()
        picked = []
        for subnet in subnets:
            az = subnet.get("AvailabilityZone")
            subnet_id = subnet.get("SubnetId")
            if not subnet_id:
                continue
            if az and az not in seen_az:
                seen_az.add(az)
                picked.append(subnet_id)
        if len(picked) < int(min_count):
            for subnet in subnets:
                subnet_id = subnet.get("SubnetId")
                if not subnet_id or subnet_id in picked:
                    continue
                picked.append(subnet_id)
                if len(picked) >= int(min_count):
                    break

        if len(picked) < int(min_count):
            return {
                "success": False,
                "error": f"EKS requires at least {min_count} subnets; only found {len(picked)} in VPC {target_vpc}.",
                "vpc_id": target_vpc,
                "subnet_ids": picked,
            }

        return {"success": True, "vpc_id": target_vpc, "subnet_ids": picked[: max(int(min_count), 2)]}

    async def wait_for_eks_cluster_active(
        self,
        cluster_name: str,
        timeout_seconds: int = 1800,
        poll_seconds: int = 30,
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not cluster_name:
            return {"success": False, "error": "cluster_name is required"}

        eks = self._client("eks")
        waited = 0
        last_status = ""
        while waited <= max(int(timeout_seconds or 0), 0):
            try:
                cluster = eks.describe_cluster(name=cluster_name).get("cluster", {})
            except Exception as e:
                return {"success": False, "error": str(e), "cluster_name": cluster_name}

            last_status = str(cluster.get("status") or "")
            state = last_status.upper()
            if state == "ACTIVE":
                return {
                    "success": True,
                    "cluster_name": cluster_name,
                    "status": state,
                    "version": cluster.get("version"),
                    "endpoint": cluster.get("endpoint"),
                    "waited_seconds": waited,
                }
            if state in {"FAILED", "DELETING"}:
                return {
                    "success": False,
                    "cluster_name": cluster_name,
                    "status": state,
                    "error": f"EKS cluster is in terminal state: {state}",
                }

            await asyncio.sleep(max(int(poll_seconds or 0), 5))
            waited += max(int(poll_seconds or 0), 5)

        return {
            "success": False,
            "cluster_name": cluster_name,
            "status": last_status,
            "requires_wait": True,
            "next_retry_seconds": max(int(poll_seconds or 30), 5),
            "error": f"EKS cluster is not ACTIVE yet (status={last_status}).",
        }

    async def ensure_eks_nodegroup_role(
        self,
        environment: str = "dev",
        cluster_name: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        env_slug = re.sub(r"[^a-zA-Z0-9-]", "-", str(environment or "dev").lower()).strip("-") or "dev"
        explicit = str(cluster_name or "").strip()
        role_name = f"{explicit}-eks-node-role" if explicit else f"AIAgent-EKSNodeRole-{env_slug}"
        return await self.ensure_service_role(
            service_slug="EKSNodegroup",
            service_principal="ec2.amazonaws.com",
            policy_arns=[
                "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
            ],
            environment=environment,
            role_name=role_name[:64],
            tags=tags,
        )

    async def _select_eks_nodegroup_subnets(
        self,
        cluster_name: str,
        private_workers: bool = True,
        min_count: int = 2,
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        eks = self._client("eks")
        ec2 = self._client("ec2")
        cluster = eks.describe_cluster(name=cluster_name).get("cluster", {})
        subnet_ids = list((cluster.get("resourcesVpcConfig") or {}).get("subnetIds") or [])
        if not subnet_ids:
            return {"success": False, "error": "EKS cluster has no VPC subnet configuration."}

        described = ec2.describe_subnets(SubnetIds=subnet_ids).get("Subnets", [])
        if not described:
            return {"success": False, "error": "Unable to describe EKS cluster subnets."}

        candidates = []
        if private_workers:
            candidates = [s for s in described if not bool(s.get("MapPublicIpOnLaunch"))]
        if len(candidates) < min_count:
            candidates = described

        picked: List[str] = []
        seen_az = set()
        for subnet in candidates:
            subnet_id = subnet.get("SubnetId")
            az = subnet.get("AvailabilityZone")
            if not subnet_id:
                continue
            if az and az not in seen_az:
                seen_az.add(az)
                picked.append(subnet_id)
        if len(picked) < min_count:
            for subnet in candidates:
                subnet_id = subnet.get("SubnetId")
                if subnet_id and subnet_id not in picked:
                    picked.append(subnet_id)
                if len(picked) >= min_count:
                    break

        if len(picked) < min_count:
            return {
                "success": False,
                "error": f"Need at least {min_count} subnets for nodegroup; found {len(picked)}.",
                "subnet_ids": picked,
            }
        return {"success": True, "subnet_ids": picked[:max(min_count, 2)]}

    async def wait_for_eks_nodegroup_active(
        self,
        cluster_name: str,
        nodegroup_name: str,
        timeout_seconds: int = 1800,
        poll_seconds: int = 30,
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        eks = self._client("eks")
        waited = 0
        last_status = ""
        while waited <= max(int(timeout_seconds or 0), 0):
            try:
                nodegroup = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=nodegroup_name).get("nodegroup", {})
            except Exception as e:
                return {"success": False, "error": str(e), "cluster_name": cluster_name, "nodegroup_name": nodegroup_name}

            last_status = str(nodegroup.get("status") or "")
            state = last_status.upper()
            if state == "ACTIVE":
                return {
                    "success": True,
                    "cluster_name": cluster_name,
                    "nodegroup_name": nodegroup_name,
                    "status": state,
                    "waited_seconds": waited,
                }
            if state in {"CREATE_FAILED", "DELETE_FAILED", "DEGRADED"}:
                return {
                    "success": False,
                    "cluster_name": cluster_name,
                    "nodegroup_name": nodegroup_name,
                    "status": state,
                    "error": f"EKS nodegroup is in terminal state: {state}",
                }

            await asyncio.sleep(max(int(poll_seconds or 0), 5))
            waited += max(int(poll_seconds or 0), 5)

        return {
            "success": False,
            "cluster_name": cluster_name,
            "nodegroup_name": nodegroup_name,
            "status": last_status,
            "requires_wait": True,
            "next_retry_seconds": max(int(poll_seconds or 30), 5),
            "error": f"EKS nodegroup is not ACTIVE yet (status={last_status}).",
        }

    async def ensure_eks_managed_nodegroup(
        self,
        cluster_name: str,
        nodegroup_name: str,
        instance_type: str = "t3.medium",
        desired_size: int = 2,
        min_size: int = 1,
        max_size: int = 3,
        private_workers: bool = True,
        tags: Optional[Dict[str, str]] = None,
        environment: str = "dev",
        wait_for_active: bool = True,
        timeout_seconds: int = 1800,
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not cluster_name:
            return {"success": False, "error": "cluster_name is required"}
        if not nodegroup_name:
            return {"success": False, "error": "nodegroup_name is required"}

        eks = self._client("eks")
        desired = max(int(desired_size or 1), 1)
        min_nodes = max(int(min_size or 1), 1)
        max_nodes = max(int(max_size or desired), desired)

        existing_names = []
        try:
            existing_names = list(eks.list_nodegroups(clusterName=cluster_name).get("nodegroups") or [])
        except Exception:
            existing_names = []

        if nodegroup_name in existing_names:
            detail = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=nodegroup_name).get("nodegroup", {})
            status = str(detail.get("status") or "")
            if status.upper() == "ACTIVE":
                scaling = detail.get("scalingConfig") or {}
                if (
                    int(scaling.get("desiredSize") or desired) != desired
                    or int(scaling.get("minSize") or min_nodes) != min_nodes
                    or int(scaling.get("maxSize") or max_nodes) != max_nodes
                ):
                    eks.update_nodegroup_config(
                        clusterName=cluster_name,
                        nodegroupName=nodegroup_name,
                        scalingConfig={
                            "desiredSize": desired,
                            "minSize": min_nodes,
                            "maxSize": max_nodes,
                        },
                    )
                    if wait_for_active:
                        wait = await self.wait_for_eks_nodegroup_active(
                            cluster_name=cluster_name,
                            nodegroup_name=nodegroup_name,
                            timeout_seconds=timeout_seconds,
                        )
                        if not wait.get("success"):
                            return wait
                return {
                    "success": True,
                    "cluster_name": cluster_name,
                    "nodegroup_name": nodegroup_name,
                    "status": "ACTIVE",
                    "instance_type": instance_type,
                    "desired_size": desired,
                }
            if wait_for_active:
                wait = await self.wait_for_eks_nodegroup_active(
                    cluster_name=cluster_name,
                    nodegroup_name=nodegroup_name,
                    timeout_seconds=timeout_seconds,
                )
                if not wait.get("success"):
                    return wait
                return {
                    "success": True,
                    "cluster_name": cluster_name,
                    "nodegroup_name": nodegroup_name,
                    "status": "ACTIVE",
                    "instance_type": instance_type,
                    "desired_size": desired,
                }
            return {"success": True, "cluster_name": cluster_name, "nodegroup_name": nodegroup_name, "status": status}

        role_result = await self.ensure_eks_nodegroup_role(
            environment=environment,
            cluster_name=cluster_name,
            tags=tags or {},
        )
        if not role_result.get("success"):
            return {"success": False, "error": role_result.get("error") or "Failed to ensure EKS nodegroup role."}

        subnet_result = await self._select_eks_nodegroup_subnets(
            cluster_name=cluster_name,
            private_workers=bool(private_workers),
            min_count=2,
        )
        if not subnet_result.get("success"):
            return subnet_result
        subnet_ids = subnet_result.get("subnet_ids") or []

        create_tags = {k: str(v) for k, v in (tags or {}).items()}
        create_tags.setdefault("Name", nodegroup_name)
        try:
            eks.create_nodegroup(
                clusterName=cluster_name,
                nodegroupName=nodegroup_name,
                subnets=subnet_ids,
                nodeRole=role_result.get("role_arn"),
                scalingConfig={
                    "desiredSize": desired,
                    "minSize": min_nodes,
                    "maxSize": max_nodes,
                },
                instanceTypes=[str(instance_type or "t3.medium")],
                diskSize=int((tags or {}).get("NodeDiskGiB", 20) or 20),
                capacityType="ON_DEMAND",
                tags=create_tags,
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

        if wait_for_active:
            wait = await self.wait_for_eks_nodegroup_active(
                cluster_name=cluster_name,
                nodegroup_name=nodegroup_name,
                timeout_seconds=timeout_seconds,
            )
            if not wait.get("success"):
                return wait

        return {
            "success": True,
            "cluster_name": cluster_name,
            "nodegroup_name": nodegroup_name,
            "status": "ACTIVE" if wait_for_active else "CREATING",
            "instance_type": instance_type,
            "desired_size": desired,
            "subnet_ids": subnet_ids,
        }

    def _eks_bearer_token(self, cluster_name: str) -> str:
        session = self.session._session
        sts_client = self._client("sts")
        signer = RequestSigner(
            sts_client.meta.service_model.service_id,
            self.region,
            "sts",
            "v4",
            session.get_credentials(),
            session.get_component("event_emitter"),
        )
        request = {
            "method": "GET",
            "url": f"https://sts.{self.region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
            "body": {},
            "headers": {"x-k8s-aws-id": cluster_name},
            "context": {},
        }
        signed_url = signer.generate_presigned_url(request, region_name=self.region, expires_in=60, operation_name="")
        token = "k8s-aws-v1." + base64.urlsafe_b64encode(signed_url.encode("utf-8")).decode("utf-8").rstrip("=")
        return token

    def _build_eks_k8s_clients(self, cluster_name: str):
        try:
            from kubernetes import client as k8s_client
        except Exception as e:
            raise RuntimeError(f"Kubernetes client is not installed: {e}") from e

        eks = self._client("eks")
        cluster = eks.describe_cluster(name=cluster_name).get("cluster", {})
        endpoint = cluster.get("endpoint")
        ca_data = ((cluster.get("certificateAuthority") or {}).get("data") or "").strip()
        if not endpoint or not ca_data:
            raise RuntimeError("EKS cluster endpoint/certificate not available yet.")

        token = self._eks_bearer_token(cluster_name)
        cert_file = tempfile.NamedTemporaryFile(mode="wb", delete=False)
        cert_file.write(base64.b64decode(ca_data))
        cert_file.flush()
        cert_file.close()

        cfg = k8s_client.Configuration()
        cfg.host = endpoint
        cfg.verify_ssl = True
        cfg.ssl_ca_cert = cert_file.name
        cfg.api_key = {"authorization": f"Bearer {token}"}
        cfg.api_key_prefix = {"authorization": "Bearer"}

        api_client = k8s_client.ApiClient(cfg)
        return (
            k8s_client,
            k8s_client.CoreV1Api(api_client),
            k8s_client.AppsV1Api(api_client),
            cert_file.name,
        )

    async def deploy_sample_website_to_eks(
        self,
        cluster_name: str,
        namespace: str = "school-site",
        app_name: str = "school-website",
        image: str = "nginx:1.25-alpine",
        timeout_seconds: int = 900,
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}

        cert_path = None
        try:
            k8s_client, core_v1, apps_v1, cert_path = self._build_eks_k8s_clients(cluster_name)

            # Namespace
            try:
                core_v1.read_namespace(name=namespace)
            except Exception:
                ns = k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=namespace))
                core_v1.create_namespace(ns)

            html = (
                "<html><head><title>School Website</title></head>"
                "<body><h1>School Website Ready</h1><p>Deployed automatically by Infra Execution Agent.</p></body></html>"
            )
            cm_name = f"{app_name}-html"
            cm = k8s_client.V1ConfigMap(
                metadata=k8s_client.V1ObjectMeta(name=cm_name, namespace=namespace),
                data={"index.html": html},
            )
            try:
                core_v1.replace_namespaced_config_map(name=cm_name, namespace=namespace, body=cm)
            except Exception:
                core_v1.create_namespaced_config_map(namespace=namespace, body=cm)

            labels = {"app": app_name}
            dep = k8s_client.V1Deployment(
                metadata=k8s_client.V1ObjectMeta(name=app_name, namespace=namespace),
                spec=k8s_client.V1DeploymentSpec(
                    replicas=1,
                    selector=k8s_client.V1LabelSelector(match_labels=labels),
                    template=k8s_client.V1PodTemplateSpec(
                        metadata=k8s_client.V1ObjectMeta(labels=labels),
                        spec=k8s_client.V1PodSpec(
                            containers=[
                                k8s_client.V1Container(
                                    name="web",
                                    image=image,
                                    ports=[k8s_client.V1ContainerPort(container_port=80)],
                                    volume_mounts=[
                                        k8s_client.V1VolumeMount(
                                            name="html-volume",
                                            mount_path="/usr/share/nginx/html/index.html",
                                            sub_path="index.html",
                                        )
                                    ],
                                )
                            ],
                            volumes=[
                                k8s_client.V1Volume(
                                    name="html-volume",
                                    config_map=k8s_client.V1ConfigMapVolumeSource(name=cm_name),
                                )
                            ],
                        ),
                    ),
                ),
            )
            try:
                apps_v1.replace_namespaced_deployment(name=app_name, namespace=namespace, body=dep)
            except Exception:
                apps_v1.create_namespaced_deployment(namespace=namespace, body=dep)

            svc = k8s_client.V1Service(
                metadata=k8s_client.V1ObjectMeta(name=app_name, namespace=namespace),
                spec=k8s_client.V1ServiceSpec(
                    selector=labels,
                    ports=[k8s_client.V1ServicePort(name="http", port=80, target_port=80)],
                    type="LoadBalancer",
                ),
            )
            try:
                core_v1.replace_namespaced_service(name=app_name, namespace=namespace, body=svc)
            except Exception:
                core_v1.create_namespaced_service(namespace=namespace, body=svc)

            waited = 0
            external = None
            while waited <= max(int(timeout_seconds or 0), 0):
                live = core_v1.read_namespaced_service(name=app_name, namespace=namespace)
                ingress = ((live.status or {}).load_balancer or {}).ingress or []
                if ingress:
                    item = ingress[0]
                    external = getattr(item, "hostname", None) or getattr(item, "ip", None)
                    if external:
                        break
                await asyncio.sleep(15)
                waited += 15

            if not external:
                return {
                    "success": False,
                    "requires_wait": True,
                    "next_retry_seconds": 30,
                    "error": "Kubernetes service is created but LoadBalancer endpoint is not ready yet.",
                    "namespace": namespace,
                    "service_name": app_name,
                }

            url = f"http://{external}"
            health = {"success": False, "status_code": None}
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    health = {"success": True, "status_code": int(getattr(resp, "status", 200) or 200)}
            except Exception:
                health = {"success": False, "status_code": None}

            return {
                "success": True,
                "namespace": namespace,
                "deployment_name": app_name,
                "service_name": app_name,
                "public_url": url,
                "health_check": health,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            if cert_path:
                try:
                    os.unlink(cert_path)
                except Exception:
                    pass

    async def wait_for_rds_instance_status(
        self,
        db_instance_id: str,
        target_status: str = "available",
        timeout_seconds: int = 1800,
        poll_seconds: int = 30,
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not db_instance_id:
            return {"success": False, "error": "db_instance_id is required"}

        rds = self._client("rds")
        target = str(target_status or "available").strip().lower()
        waited = 0
        while waited <= max(int(timeout_seconds or 0), 0):
            try:
                db = rds.describe_db_instances(DBInstanceIdentifier=db_instance_id)["DBInstances"][0]
            except Exception as e:
                return {"success": False, "error": str(e), "db_instance_id": db_instance_id}

            status = str(db.get("DBInstanceStatus") or "").strip().lower()
            if status == target:
                return {
                    "success": True,
                    "ready": True,
                    "db_instance_id": db_instance_id,
                    "status": db.get("DBInstanceStatus"),
                    "endpoint": (db.get("Endpoint") or {}).get("Address"),
                    "port": (db.get("Endpoint") or {}).get("Port"),
                }
            if status in {"deleting", "failed", "incompatible-restore", "incompatible-network"}:
                return {
                    "success": False,
                    "db_instance_id": db_instance_id,
                    "status": db.get("DBInstanceStatus"),
                    "error": f"RDS instance entered terminal state: {db.get('DBInstanceStatus')}",
                }
            await asyncio.sleep(max(int(poll_seconds or 0), 5))
            waited += max(int(poll_seconds or 0), 5)

        return {
            "success": False,
            "ready": False,
            "db_instance_id": db_instance_id,
            "error": f"Timed out waiting for RDS instance to reach {target_status}",
            "next_retry_seconds": max(int(poll_seconds or 0), 15),
        }

    async def wait_for_lambda_active(
        self,
        function_name: str,
        timeout_seconds: int = 300,
        poll_seconds: int = 10,
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not function_name:
            return {"success": False, "error": "function_name is required"}

        lam = self._client("lambda")
        waited = 0
        while waited <= max(int(timeout_seconds or 0), 0):
            try:
                config = lam.get_function(FunctionName=function_name).get("Configuration", {})
            except Exception as e:
                return {"success": False, "error": str(e), "function_name": function_name}
            state = str(config.get("State") or "Unknown")
            lower = state.lower()
            if lower in {"active", "unknown"}:
                return {
                    "success": True,
                    "ready": True,
                    "function_name": function_name,
                    "state": state,
                    "arn": config.get("FunctionArn"),
                }
            if lower in {"failed", "inactive"}:
                return {
                    "success": False,
                    "function_name": function_name,
                    "state": state,
                    "error": f"Lambda entered terminal state: {state}",
                }
            await asyncio.sleep(max(int(poll_seconds or 0), 5))
            waited += max(int(poll_seconds or 0), 5)

        return {
            "success": False,
            "ready": False,
            "function_name": function_name,
            "error": "Timed out waiting for Lambda function to become active",
            "next_retry_seconds": max(int(poll_seconds or 0), 10),
        }

    async def run_s3_workflow(
        self,
        action: str,
        bucket_name: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None,
        environment: str = "dev",
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        params = dict(parameters or {})
        tags = tags or {}
        action_l = str(action or "create").strip().lower()
        if action_l not in {"create", "update"}:
            return {"success": False, "error": f"Unsupported s3 workflow action: {action_l}"}
        if action_l == "update" and not bucket_name:
            return {"success": False, "error": "bucket_name is required for s3 update workflow"}

        result = await self.execute(
            action=action_l,
            resource_type="s3",
            resource_name=bucket_name,
            parameters=params,
            tags=tags,
        )
        if not result.get("success"):
            return result

        result.setdefault("action", action_l)
        result.setdefault("resource_type", "s3")
        bucket = result.get("bucket_name") or bucket_name
        if bucket:
            result["bucket_name"] = bucket

        website_requested = bool(
            params.get("website_configuration")
            or params.get("website_enabled")
            or result.get("website_configuration")
        )
        if website_requested and not result.get("website_url") and bucket:
            result["website_url"] = f"http://{bucket}.s3-website-{self.region}.amazonaws.com"
        if website_requested:
            result["deploy_completed"] = True
            result.setdefault("final_outcome", "S3 website workflow completed.")

        wait_for_website = self._to_bool(params.get("wait_for_website"), default=False)
        website_url = result.get("website_url")
        if website_requested and website_url and wait_for_website:
            timeout_seconds = int(params.get("website_wait_timeout_seconds") or 180)
            waited = 0
            reachable = False
            last_error = ""
            while waited <= max(timeout_seconds, 0):
                try:
                    with urllib.request.urlopen(str(website_url), timeout=10) as resp:
                        status_code = int(getattr(resp, "status", 200) or 200)
                        result["health_check"] = {"success": status_code < 500, "status_code": status_code}
                        reachable = status_code < 500
                        if reachable:
                            break
                except Exception as e:
                    last_error = str(e)
                await asyncio.sleep(10)
                waited += 10
            if not reachable:
                result["readiness"] = "website_propagating"
                result["next_retry_seconds"] = 30
                if last_error:
                    result["health_check_error"] = last_error

        return result

    async def run_lambda_workflow(
        self,
        action: str,
        function_name: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None,
        environment: str = "dev",
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        params = dict(parameters or {})
        tags = tags or {}
        action_l = str(action or "create").strip().lower()
        if action_l not in {"create", "update"}:
            return {"success": False, "error": f"Unsupported lambda workflow action: {action_l}"}
        if action_l == "update" and not function_name:
            return {"success": False, "error": "function_name is required for lambda update workflow"}

        result = await self.execute(
            action=action_l,
            resource_type="lambda",
            resource_name=function_name,
            parameters=params,
            tags=tags,
        )
        if not result.get("success"):
            return result

        fn_name = result.get("function_name") or function_name
        if fn_name:
            result["function_name"] = fn_name
        result.setdefault("action", action_l)
        result.setdefault("resource_type", "lambda")

        wait_for_active = self._to_bool(params.get("wait_for_active"), default=True)
        if fn_name and wait_for_active:
            wait = await self.wait_for_lambda_active(
                function_name=str(fn_name),
                timeout_seconds=int(params.get("lambda_wait_timeout_seconds") or 300),
                poll_seconds=int(params.get("lambda_poll_seconds") or 10),
            )
            if not wait.get("success"):
                if wait.get("ready") is False:
                    result["readiness"] = "lambda_initializing"
                    result["next_retry_seconds"] = wait.get("next_retry_seconds", 15)
                    result["state"] = wait.get("state", "Pending")
                    result.setdefault("success", True)
                    return result
                wait.setdefault("action", action_l)
                wait.setdefault("resource_type", "lambda")
                wait.setdefault("function_name", fn_name)
                return wait
            result["state"] = wait.get("state", "Active")
            result["arn"] = result.get("arn") or wait.get("arn")

        if self._to_bool(params.get("invoke_test"), default=False) and fn_name:
            lam = self._client("lambda")
            payload = params.get("invoke_payload")
            if payload in (None, "", []):
                payload = {"health_check": True}
            if not isinstance(payload, (str, bytes)):
                payload = json.dumps(payload)
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            try:
                invoke = lam.invoke(FunctionName=str(fn_name), InvocationType="RequestResponse", Payload=payload)
                result["invoke_status_code"] = int(invoke.get("StatusCode") or 0)
            except Exception as e:
                result["invoke_error"] = str(e)

        return result

    async def run_rds_workflow(
        self,
        action: str,
        db_instance_id: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None,
        environment: str = "dev",
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        params = dict(parameters or {})
        tags = tags or {}
        action_l = str(action or "create").strip().lower()
        if action_l not in {"create", "update"}:
            return {"success": False, "error": f"Unsupported rds workflow action: {action_l}"}
        if action_l == "update" and not db_instance_id:
            return {"success": False, "error": "db_instance_id is required for rds update workflow"}

        result = await self.execute(
            action=action_l,
            resource_type="rds",
            resource_name=db_instance_id,
            parameters=params,
            tags=tags,
        )
        if not result.get("success"):
            return result

        db_id = result.get("db_instance_id") or db_instance_id
        if db_id:
            result["db_instance_id"] = db_id
        result.setdefault("action", action_l)
        result.setdefault("resource_type", "rds")

        wait_for_available = self._to_bool(params.get("wait_for_available"), default=True)
        if db_id and wait_for_available:
            wait = await self.wait_for_rds_instance_status(
                db_instance_id=str(db_id),
                target_status="available",
                timeout_seconds=int(params.get("rds_wait_timeout_seconds") or 1800),
                poll_seconds=int(params.get("rds_poll_seconds") or 30),
            )
            if not wait.get("success"):
                wait.setdefault("action", action_l)
                wait.setdefault("resource_type", "rds")
                wait.setdefault("db_instance_id", db_id)
                wait["partial_result"] = result
                return wait
            result["status"] = wait.get("status", "available")
            result["endpoint"] = result.get("endpoint") or wait.get("endpoint")
            result["port"] = result.get("port") or wait.get("port")
        elif db_id:
            describe = await self.execute(
                action="describe",
                resource_type="rds",
                resource_name=str(db_id),
                parameters={},
                tags={},
            )
            if describe.get("success"):
                status = str(describe.get("status") or "unknown")
                result["status"] = status
                result["endpoint"] = describe.get("endpoint")
                result["port"] = describe.get("port")
                if status.lower() != "available":
                    result["readiness"] = "rds_initializing"
                    result["next_retry_seconds"] = 30

        run_sql = bool(params.get("run_sql_via_ssm") or params.get("sql") or params.get("sql_statements"))
        if run_sql and db_id:
            current_status = str(result.get("status") or "").lower()
            if current_status and current_status != "available":
                result["readiness"] = "rds_initializing"
                result["next_retry_seconds"] = 30
                result["message"] = "RDS instance is not available yet. SQL stage will run after DB becomes available."
                return result

            sql_value = params.get("sql") or params.get("sql_statements")
            if isinstance(sql_value, list):
                sql_text = ";\n".join([str(item).strip().rstrip(";") for item in sql_value if str(item).strip()])
                if sql_text:
                    sql_text = sql_text + ";"
            else:
                sql_text = str(sql_value or "").strip()

            if sql_text:
                sql_result = await self.execute_rds_sql_via_ssm(
                    rds_instance_id=str(db_id),
                    sql=sql_text,
                    db_name=params.get("db_name"),
                    db_username=params.get("db_username"),
                    db_password=params.get("db_password"),
                    secret_arn=params.get("secret_arn"),
                    bastion_instance_id=None if str(params.get("bastion_instance_id", "")).strip().lower() == "auto" else params.get("bastion_instance_id"),
                    ensure_network_path=self._to_bool(params.get("ensure_network_path"), default=True),
                    wait_seconds=int(params.get("wait_seconds") or 300),
                )
                result["sql_result"] = sql_result
                if not sql_result.get("success"):
                    return {
                        "success": False,
                        "action": "update",
                        "resource_type": "rds",
                        "db_instance_id": db_id,
                        "error": sql_result.get("error") or "RDS SQL stage failed.",
                        "sql_result": sql_result,
                    }
                result["action"] = "update"
                result["final_outcome"] = "RDS update and SQL deployment completed."

        return result

    async def run_eks_workflow(
        self,
        cluster_name: str,
        parameters: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None,
        environment: str = "dev",
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        params = dict(parameters or {})
        tags = tags or {}
        if not cluster_name:
            return {"success": False, "error": "cluster_name is required"}

        wait_for_active = self._to_bool(params.get("wait_for_active"), default=True)
        wait_for_nodegroup = self._to_bool(params.get("wait_for_nodegroup"), default=True)
        deploy_sample = self._to_bool(params.get("deploy_sample_website"), default=True)
        expose_public = self._to_bool(params.get("expose_public_url"), default=True)
        private_workers = self._to_bool(params.get("private_workers"), default=True)
        workflow_timeout = int(params.get("workflow_timeout_seconds") or 1800)

        eks = self._client("eks")
        created_now = False
        try:
            cluster = eks.describe_cluster(name=cluster_name).get("cluster", {})
        except Exception as e:
            text = str(e or "")
            if "ResourceNotFoundException" in text or "No cluster found" in text:
                create_if_missing = self._to_bool(params.get("create_if_missing"), default=True)
                if not create_if_missing:
                    return {"success": False, "error": text, "cluster_name": cluster_name}
                create_result = await self._create_eks(
                    resource_name=cluster_name,
                    parameters=params,
                    tags=tags,
                )
                if not create_result.get("success"):
                    return create_result
                created_now = True
                cluster = {"status": "CREATING"}
            else:
                return {"success": False, "error": text, "cluster_name": cluster_name}

        status = str(cluster.get("status") or "").upper()
        if status != "ACTIVE":
            if not wait_for_active:
                return {
                    "success": True,
                    "action": "update",
                    "resource_type": "eks",
                    "cluster_name": cluster_name,
                    "status": status,
                    "readiness": "cluster_initializing",
                    "next_retry_seconds": 30,
                    "created_now": created_now,
                }
            wait_cluster = await self.wait_for_eks_cluster_active(
                cluster_name=cluster_name,
                timeout_seconds=workflow_timeout,
            )
            if not wait_cluster.get("success"):
                return wait_cluster
            status = "ACTIVE"

        nodegroup_name = str(params.get("nodegroup_name") or f"{cluster_name[:28]}-ng").strip()
        node_instance_type = str(
            params.get("node_instance_type")
            or params.get("instance_type")
            or "t3.medium"
        ).strip()
        node_count = int(params.get("node_count") or params.get("desired_nodes") or params.get("desired_size") or 2)
        min_nodes = int(params.get("node_min_size") or max(1, min(node_count, 2)))
        max_nodes = int(params.get("node_max_size") or max(node_count, 3))

        nodegroup = await self.ensure_eks_managed_nodegroup(
            cluster_name=cluster_name,
            nodegroup_name=nodegroup_name,
            instance_type=node_instance_type,
            desired_size=node_count,
            min_size=min_nodes,
            max_size=max_nodes,
            private_workers=private_workers,
            tags=tags,
            environment=environment,
            wait_for_active=wait_for_nodegroup,
            timeout_seconds=workflow_timeout,
        )
        if not nodegroup.get("success"):
            return nodegroup

        result: Dict[str, Any] = {
            "success": True,
            "action": "create" if created_now else "update",
            "resource_type": "eks",
            "cluster_name": cluster_name,
            "status": status,
            "nodegroup_name": nodegroup_name,
            "nodegroup_status": nodegroup.get("status", "ACTIVE"),
            "node_instance_type": node_instance_type,
            "node_count": node_count,
            "created_now": created_now,
        }

        if deploy_sample and expose_public:
            deploy = await self.deploy_sample_website_to_eks(
                cluster_name=cluster_name,
                namespace=str(params.get("namespace") or "school-site"),
                app_name=str(params.get("app_name") or "school-website"),
                image=str(params.get("website_image") or "nginx:1.25-alpine"),
                timeout_seconds=int(params.get("app_deploy_timeout_seconds") or 900),
            )
            result["deploy_result"] = deploy
            if deploy.get("success"):
                result["public_url"] = deploy.get("public_url")
                result["health_check"] = deploy.get("health_check")
                result["final_outcome"] = "EKS cluster, node group, and sample website deployment completed."
            else:
                result["success"] = False
                result["error"] = deploy.get("error") or "Website deployment on EKS failed."

        return result

    async def discover_default_network_context(self, min_subnets: int = 1) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        ec2 = self._client("ec2")
        vpc_id = self._resolve_default_vpc_id(ec2)
        if not vpc_id:
            return {"success": False, "error": "No default VPC found."}

        subnets = ec2.describe_subnets(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "state", "Values": ["available"]},
            ]
        ).get("Subnets", [])
        subnet_ids = [s.get("SubnetId") for s in subnets if s.get("SubnetId")]

        security_groups = ec2.describe_security_groups(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "group-name", "Values": ["default"]},
            ]
        ).get("SecurityGroups", [])
        default_sg = security_groups[0].get("GroupId") if security_groups else None

        if len(subnet_ids) < int(min_subnets):
            return {
                "success": False,
                "error": f"Default VPC {vpc_id} has only {len(subnet_ids)} subnet(s).",
                "vpc_id": vpc_id,
                "subnet_ids": subnet_ids,
                "default_security_group_id": default_sg,
            }

        return {
            "success": True,
            "vpc_id": vpc_id,
            "subnet_ids": subnet_ids,
            "default_security_group_id": default_sg,
        }

    async def ensure_service_role(
        self,
        service_slug: str,
        service_principal: str,
        policy_arns: List[str],
        environment: str = "dev",
        role_name: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}

        env_slug = re.sub(r"[^a-zA-Z0-9-]", "-", str(environment or "dev").lower()).strip("-") or "dev"
        derived_name = role_name or f"AIAgent-{service_slug}-Role-{env_slug}"
        derived_name = re.sub(r"[^a-zA-Z0-9+=,.@_-]", "-", derived_name)[:64]
        iam = self._client("iam")

        trust_policy = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": service_principal},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        )

        role_created = False
        discovered_existing = False
        role_arn = None
        try:
            resp = iam.get_role(RoleName=derived_name)
            role_arn = (resp.get("Role") or {}).get("Arn")
        except Exception:
            try:
                create_kwargs: Dict[str, Any] = {
                    "RoleName": derived_name,
                    "AssumeRolePolicyDocument": trust_policy,
                    "Description": f"Managed by AI Infra Platform for {service_slug}.",
                }
                tag_items = [{"Key": str(k), "Value": str(v)} for k, v in (tags or {}).items()]
                if tag_items:
                    create_kwargs["Tags"] = tag_items
                resp = iam.create_role(**create_kwargs)
                role_arn = (resp.get("Role") or {}).get("Arn")
                role_created = True
            except Exception as e:
                discovered = await self.discover_existing_service_role(
                    service_principal=service_principal,
                    role_name_hint=derived_name,
                )
                if not discovered.get("success"):
                    return {"success": False, "error": str(e)}
                derived_name = str(discovered.get("role_name") or derived_name)
                role_arn = discovered.get("role_arn")
                discovered_existing = True

        policy_results = []
        warnings = []
        for policy_arn in policy_arns:
            attach = await self.ensure_iam_policy_attached(role_name=derived_name, policy_arn=policy_arn)
            policy_results.append(attach)
            if not attach.get("success"):
                text = str(attach.get("error") or "")
                if discovered_existing and "accessdenied" in text.lower():
                    warnings.append(text)
                    continue
                return attach

        return {
            "success": True,
            "role_name": derived_name,
            "role_arn": role_arn,
            "role_created": role_created,
            "discovered_existing": discovered_existing,
            "policies_attached": policy_results,
            "warnings": warnings,
        }

    def _role_requirements_for_resource(self, resource_type: str, parameters: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        rt = str(resource_type or "").strip().lower()
        params = parameters or {}
        if rt == "eks":
            return {
                "service_slug": "EKSCluster",
                "service_principal": "eks.amazonaws.com",
                "policy_arns": [
                    "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                    "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController",
                ],
            }
        if rt == "lambda":
            return {
                "service_slug": "LambdaExec",
                "service_principal": "lambda.amazonaws.com",
                "policy_arns": ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"],
            }
        if rt == "stepfunctions":
            return {
                "service_slug": "StepFunctionsExec",
                "service_principal": "states.amazonaws.com",
                "policy_arns": ["arn:aws:iam::aws:policy/AWSStepFunctionsFullAccess"],
            }
        if rt == "sagemaker":
            return {
                "service_slug": "SageMakerExec",
                "service_principal": "sagemaker.amazonaws.com",
                "policy_arns": ["arn:aws:iam::aws:policy/AmazonSageMakerFullAccess"],
            }
        if rt == "glue" and str(params.get("glue_type", "database")).strip().lower() == "crawler":
            return {
                "service_slug": "GlueCrawlerExec",
                "service_principal": "glue.amazonaws.com",
                "policy_arns": ["arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"],
            }
        if rt == "codepipeline":
            return {
                "service_slug": "CodePipelineExec",
                "service_principal": "codepipeline.amazonaws.com",
                "policy_arns": ["arn:aws:iam::aws:policy/AWSCodePipeline_FullAccess"],
            }
        if rt == "codebuild":
            return {
                "service_slug": "CodeBuildExec",
                "service_principal": "codebuild.amazonaws.com",
                "policy_arns": ["arn:aws:iam::aws:policy/AWSCodeBuildDeveloperAccess"],
            }
        return None

    def _infer_service_principal(self, service_name: str) -> str:
        normalized = str(service_name or "").strip().lower()
        overrides = {
            "elbv2": "elasticloadbalancing.amazonaws.com",
            "elb": "elasticloadbalancing.amazonaws.com",
            "events": "events.amazonaws.com",
            "logs": "logs.amazonaws.com",
            "states": "states.amazonaws.com",
            "stepfunctions": "states.amazonaws.com",
            "apigateway": "apigateway.amazonaws.com",
            "wafv2": "wafv2.amazonaws.com",
            "route53": "route53.amazonaws.com",
            "codepipeline": "codepipeline.amazonaws.com",
            "codebuild": "codebuild.amazonaws.com",
        }
        if normalized in overrides:
            return overrides[normalized]
        if normalized.endswith(".amazonaws.com"):
            return normalized
        return f"{normalized}.amazonaws.com"

    async def auto_fill_intent_prerequisites(
        self,
        action: str,
        resource_type: str,
        resource_name: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None,
        environment: str = "dev",
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}

        action_l = str(action or "").strip().lower()
        resource_l = str(resource_type or "").strip().lower()
        params = dict(parameters or {})
        autofilled: List[str] = []
        warnings: List[str] = []
        tags = tags or {}
        custom_networking = self._to_bool(
            params.get("use_custom_networking", params.get("custom_networking")),
            default=False,
        )
        network_profile = str(params.get("network_profile", "")).strip().lower()
        if network_profile in {"custom", "manual", "advanced"}:
            custom_networking = True

        # Treat placeholder values as auto/default so network context can be auto-filled.
        for key in ["vpc_id", "subnet_id"]:
            val = params.get(key)
            if isinstance(val, str) and val.strip().lower() in {"default", "auto", "automatic", "none", "null"}:
                params.pop(key, None)
        for key in ["subnet_ids", "security_group_ids", "securityGroupIds", "vpc_security_group_ids"]:
            val = params.get(key)
            if isinstance(val, str) and val.strip().lower() in {"default", "auto", "automatic", "none", "null"}:
                params.pop(key, None)

        if action_l not in {"create", "update"} or not resource_l:
            return {"success": True, "parameters": params, "autofilled": autofilled, "warnings": warnings}

        service_name = self._schema_helper.SERVICE_ALIASES.get(resource_l, resource_l)
        operation_name = self._schema_helper._resolve_operation_name(service_name, resource_l, action_l)
        required_members: List[str] = []
        try:
            if operation_name:
                op_model = self._client(service_name).meta.service_model.operation_model(operation_name)
                input_shape = op_model.input_shape
                required_members = list(getattr(input_shape, "required_members", []) or []) if input_shape else []
        except Exception:
            required_members = []

        required_lower = {str(item).lower() for item in required_members}
        role_fields_lower = {"role", "rolearn", "executionrolearn", "servicerole"}
        role_req = self._role_requirements_for_resource(resource_l, params)
        need_role = bool(
            params.get("role_arn") in (None, "", [])
            and required_lower.intersection(role_fields_lower)
        )
        if need_role:
            try:
                if role_req and resource_l == "eks":
                    role_result = await self.ensure_eks_cluster_role(environment=environment, cluster_name=resource_name, tags=tags)
                elif role_req:
                    role_name_hint = f"{resource_name}-{resource_l}-role" if resource_name else None
                    role_result = await self.ensure_service_role(
                        service_slug=str(role_req.get("service_slug")),
                        service_principal=str(role_req.get("service_principal")),
                        policy_arns=list(role_req.get("policy_arns") or []),
                        environment=environment,
                        role_name=role_name_hint,
                        tags=tags,
                    )
                else:
                    inferred_principal = self._infer_service_principal(service_name)
                    discovered = await self.discover_existing_service_role(
                        service_principal=inferred_principal,
                        role_name_hint=str(resource_name or resource_l),
                    )
                    if discovered.get("success") and discovered.get("role_arn"):
                        params["role_arn"] = discovered.get("role_arn")
                        autofilled.append("role_arn")
                        role_result = {"success": True}
                    else:
                        role_name_hint = f"{resource_name}-{service_name}-role" if resource_name else None
                        role_result = await self.ensure_service_role(
                            service_slug=f"{service_name}-auto",
                            service_principal=inferred_principal,
                            policy_arns=[],
                            environment=environment,
                            role_name=role_name_hint,
                            tags=tags,
                        )
                        if role_result.get("success") and role_result.get("role_arn"):
                            params["role_arn"] = role_result.get("role_arn")
                            autofilled.append("role_arn")
                            warnings.append(
                                f"Auto-created role for {service_name} without managed policies. "
                                "Service-specific execution permissions may still be required."
                            )
                if role_result.get("error"):
                    warnings.append(str(role_result.get("error")))
            except Exception as e:
                warnings.append(str(e))

        if resource_l == "lambda":
            if params.get("runtime") in (None, "", []):
                params["runtime"] = "python3.12"
                autofilled.append("runtime")
            if params.get("timeout") in (None, "", []):
                params["timeout"] = 30
                autofilled.append("timeout")
            if params.get("memory") in (None, "", []):
                params["memory"] = 128
                autofilled.append("memory")
            if params.get("code") in (None, "", []):
                params["code"] = "def handler(event, context):\n    return {'statusCode': 200, 'body': 'Hello from AI Platform'}"
                autofilled.append("code")

        if resource_l == "ec2":
            if params.get("instance_type") in (None, "", []):
                params["instance_type"] = "t3.micro"
                autofilled.append("instance_type")
            if params.get("ami_id") in (None, "", []):
                try:
                    ec2 = self._client("ec2")
                    query = self._resolve_ami_query(params.get("os_flavor"))
                    images = ec2.describe_images(**query).get("Images", [])
                    images = sorted(images, key=lambda item: item.get("CreationDate", ""), reverse=True)
                    if images:
                        params["ami_id"] = images[0].get("ImageId")
                        autofilled.append("ami_id")
                except Exception as e:
                    warnings.append(f"AMI auto-discovery failed: {e}")

        needs_network = bool(
            required_lower.intersection({"vpcid", "subnetid", "subnetids", "securitygroupids", "resourcesvpcconfig"})
            or resource_l in {"eks", "elb", "ecs"}
        )
        if needs_network and not custom_networking:
            min_subnets = 2 if resource_l in {"eks", "elb"} else 1
            net = await self.discover_default_network_context(min_subnets=min_subnets)
            if net.get("success"):
                if params.get("vpc_id") in (None, "", []) and net.get("vpc_id"):
                    params["vpc_id"] = net.get("vpc_id")
                    autofilled.append("vpc_id")

                existing_subnets = self._to_list(params.get("subnet_ids") or params.get("subnetIds"))
                if len(existing_subnets) < min_subnets:
                    picked = self._to_list(net.get("subnet_ids"))[: max(min_subnets, 2)]
                    if picked:
                        params["subnet_ids"] = picked
                        autofilled.append("subnet_ids")

                if params.get("subnet_id") in (None, "", []):
                    first_subnet = self._to_list(params.get("subnet_ids"))[:1]
                    if first_subnet:
                        params["subnet_id"] = first_subnet[0]
                        autofilled.append("subnet_id")

                existing_sg = self._to_list(params.get("security_group_ids") or params.get("securityGroupIds"))
                if not existing_sg and net.get("default_security_group_id"):
                    params["security_group_ids"] = [net.get("default_security_group_id")]
                    autofilled.append("security_group_ids")
            elif net.get("error"):
                warnings.append(str(net.get("error")))

        if "resourcesvpcconfig" in required_lower and resource_l == "eks":
            subnet_ids = self._to_list(params.get("subnet_ids"))
            if len(subnet_ids) >= 2:
                cfg = params.get("resources_vpc_config")
                if not isinstance(cfg, dict):
                    cfg = {}
                cfg.setdefault("subnetIds", subnet_ids)
                sg_ids = self._to_list(params.get("security_group_ids"))
                if sg_ids:
                    cfg.setdefault("securityGroupIds", sg_ids)
                params["resources_vpc_config"] = cfg
                autofilled.append("resources_vpc_config")

        if "mincount" in required_lower and params.get("MinCount") in (None, "", []):
            params["MinCount"] = 1
            autofilled.append("MinCount")
        if "maxcount" in required_lower and params.get("MaxCount") in (None, "", []):
            params["MaxCount"] = 1
            autofilled.append("MaxCount")

        return {
            "success": True,
            "parameters": params,
            "autofilled": sorted(set(autofilled)),
            "warnings": warnings,
            "operation": operation_name,
        }

    async def attach_instance_profile(self, instance_id: str, profile_name: str) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not instance_id or not profile_name:
            return {"success": False, "error": "instance_id and profile_name are required"}
        ec2 = self._client("ec2")
        try:
            associations = ec2.describe_iam_instance_profile_associations(
                Filters=[{"Name": "instance-id", "Values": [instance_id]}]
            ).get("IamInstanceProfileAssociations", [])
        except Exception as e:
            return {"success": False, "error": str(e)}

        current = associations[0] if associations else None
        if current:
            current_name = (current.get("IamInstanceProfile", {}) or {}).get("Arn", "")
            if current_name.endswith(f"/{profile_name}"):
                return {
                    "success": True,
                    "instance_id": instance_id,
                    "profile_name": profile_name,
                    "changed": False,
                }
            try:
                ec2.replace_iam_instance_profile_association(
                    AssociationId=current.get("AssociationId"),
                    IamInstanceProfile={"Name": profile_name},
                )
                return {
                    "success": True,
                    "instance_id": instance_id,
                    "profile_name": profile_name,
                    "changed": True,
                    "mode": "replaced",
                }
            except Exception as e:
                return {"success": False, "error": str(e)}

        try:
            ec2.associate_iam_instance_profile(
                IamInstanceProfile={"Name": profile_name},
                InstanceId=instance_id,
            )
            return {
                "success": True,
                "instance_id": instance_id,
                "profile_name": profile_name,
                "changed": True,
                "mode": "associated",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def ensure_ssm_prerequisites_for_instance(
        self,
        instance_id: str,
        environment: str = "dev",
        role_model: str = "shared",
        wait_seconds: int = 300,
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not instance_id:
            return {"success": False, "error": "instance_id is required"}

        profile_result = await self.ensure_managed_instance_profile(environment=environment, role_model=role_model)
        if not profile_result.get("success"):
            return profile_result

        attach_result = await self.attach_instance_profile(
            instance_id=instance_id,
            profile_name=profile_result.get("profile_name"),
        )
        if not attach_result.get("success"):
            return attach_result

        wait_result = await self.wait_for_ssm_registration(instance_id=instance_id, timeout_seconds=wait_seconds)
        if not wait_result.get("success"):
            return wait_result

        return {
            "success": True,
            "instance_id": instance_id,
            "profile_name": profile_result.get("profile_name"),
            "role_name": profile_result.get("role_name"),
            "attached": True,
            "ssm_managed": True,
            "wait_seconds": wait_seconds,
        }

    async def ensure_service_linked_role(self, service_name: str) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not service_name:
            return {"success": False, "error": "service_name is required"}
        iam = self._client("iam")
        try:
            resp = iam.create_service_linked_role(AWSServiceName=service_name)
            return {
                "success": True,
                "service_name": service_name,
                "role_arn": (resp.get("Role") or {}).get("Arn"),
                "changed": True,
            }
        except Exception as e:
            text = str(e)
            if "InvalidInput" in text and "has been taken" in text:
                return {"success": True, "service_name": service_name, "changed": False}
            if "service-linked role" in text and "exists" in text.lower():
                return {"success": True, "service_name": service_name, "changed": False}
            return {"success": False, "error": text}

    async def ensure_security_group_ingress(
        self,
        group_id: str,
        port: int,
        protocol: str = "tcp",
        cidr: str = "0.0.0.0/0",
    ) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not group_id or int(port or 0) <= 0:
            return {"success": False, "error": "group_id and valid port are required"}
        ec2 = self._client("ec2")
        try:
            ec2.authorize_security_group_ingress(
                GroupId=group_id,
                IpPermissions=[
                    {
                        "IpProtocol": protocol,
                        "FromPort": int(port),
                        "ToPort": int(port),
                        "IpRanges": [{"CidrIp": cidr}],
                    }
                ],
            )
            return {"success": True, "group_id": group_id, "port": int(port), "changed": True}
        except Exception as e:
            if "InvalidPermission.Duplicate" in str(e):
                return {"success": True, "group_id": group_id, "port": int(port), "changed": False}
            return {"success": False, "error": str(e)}

    async def ensure_bucket_public_access_compliance(self, bucket_name: str, allow_public: bool) -> Dict[str, Any]:
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}
        if not bucket_name:
            return {"success": False, "error": "bucket_name is required"}
        s3 = self._client("s3")
        cfg = {
            "BlockPublicAcls": not bool(allow_public),
            "IgnorePublicAcls": not bool(allow_public),
            "BlockPublicPolicy": not bool(allow_public),
            "RestrictPublicBuckets": not bool(allow_public),
        }
        try:
            s3.put_public_access_block(
                Bucket=bucket_name,
                PublicAccessBlockConfiguration=cfg,
            )
            return {"success": True, "bucket_name": bucket_name, "allow_public": bool(allow_public), "changed": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def ensure_endpoint_network_association(
        self,
        service_name: str,
        resource_id: str,
        subnet_ids: Optional[List[str]] = None,
        security_group_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        # Generic placeholder for cross-service endpoint/network associations.
        return {
            "success": False,
            "requires_manual": True,
            "error": "Generic endpoint network association is service-specific and requires explicit adapter configuration.",
            "service_name": service_name,
            "resource_id": resource_id,
            "subnet_ids": subnet_ids or [],
            "security_group_ids": security_group_ids or [],
        }

    async def _authorize_rds_from_bastion(self, bastion_instance_id: str, db_instance: Dict[str, Any], db_port: int):
        ec2 = self._client("ec2")
        response = ec2.describe_instances(InstanceIds=[bastion_instance_id])
        reservations = response.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            raise ValueError(f"Bastion instance {bastion_instance_id} not found")

        bastion = reservations[0]["Instances"][0]
        bastion_sgs = [sg.get("GroupId") for sg in bastion.get("SecurityGroups", []) if sg.get("GroupId")]
        if not bastion_sgs:
            return

        rds_sgs = [sg.get("VpcSecurityGroupId") for sg in db_instance.get("VpcSecurityGroups", []) if sg.get("VpcSecurityGroupId")]
        for rds_sg in rds_sgs:
            for bastion_sg in bastion_sgs:
                try:
                    ec2.authorize_security_group_ingress(
                        GroupId=rds_sg,
                        IpPermissions=[
                            {
                                "IpProtocol": "tcp",
                                "FromPort": int(db_port),
                                "ToPort": int(db_port),
                                "UserIdGroupPairs": [{"GroupId": bastion_sg}],
                            }
                        ],
                    )
                except Exception as e:
                    if "InvalidPermission.Duplicate" in str(e):
                        continue
                    raise

    def _normalize_sql_engine_mode(self, engine: str) -> Optional[str]:
        lowered = str(engine or "").lower()
        if "postgres" in lowered:
            return "postgres"
        if any(token in lowered for token in ["mysql", "mariadb", "aurora"]):
            return "mysql"
        return None

    async def run_dynamic_boto(self, code: str) -> Dict[str, Any]:
        """
        Execute LLM-generated Python code using the active Boto3 session.
        Returns the parsed JSON result from the script's stdout.
        """
        if not self.initialized:
            return {"success": False, "error": "AWS not initialized."}

        lowered = code.lower()
        interactive_markers = ("input(", "raw_input(", "sys.stdin.read(", "stdin.readline(")
        if any(marker in lowered for marker in interactive_markers):
            return {
                "success": False,
                "error": "Generated code contains interactive stdin input calls, which are not allowed.",
                "output": "",
            }
        forbidden_import_markers = ("import psycopg2", "from psycopg2", "import sqlalchemy", "from sqlalchemy")
        if any(marker in lowered for marker in forbidden_import_markers):
            return {
                "success": False,
                "error": "Generated code imported non-boto dependencies (psycopg2/sqlalchemy). Only boto3-based automation is allowed.",
                "output": "",
            }

        print(f"  Executing dynamic Boto3 script:\n{'-'*40}\n{code}\n{'-'*40}")
        import io
        import traceback
        from contextlib import redirect_stdout

        # Captured stdout
        f = io.StringIO()
        
        # Force dynamically generated boto3.client/resource calls to use the authenticated session.
        original_boto3_client = boto3.client
        original_boto3_resource = boto3.resource
        original_boto3_session = boto3.Session

        def _session_client(service_name, *args, **kwargs):
            if not kwargs.get("region_name"):
                kwargs["region_name"] = self.region
            return self.session.client(service_name, *args, **kwargs)

        def _session_resource(service_name, *args, **kwargs):
            if not kwargs.get("region_name"):
                kwargs["region_name"] = self.region
            return self.session.resource(service_name, *args, **kwargs)

        def _session_factory(*args, **kwargs):
            if not kwargs.get("region_name"):
                kwargs["region_name"] = self.region
            if self.access_key and not kwargs.get("aws_access_key_id"):
                kwargs["aws_access_key_id"] = self.access_key
            if self.secret_key and not kwargs.get("aws_secret_access_key"):
                kwargs["aws_secret_access_key"] = self.secret_key
            return original_boto3_session(*args, **kwargs)

        boto3.client = _session_client
        boto3.resource = _session_resource
        boto3.Session = _session_factory

        # Local context for execution
        exec_globals = {
            "session": self.session,
            "boto3": boto3,
            "json": json,
            "region": self.region,
            "context": {},
            "print": print, # Ensure print is available
        }
        previous_access_key = os.environ.get("AWS_ACCESS_KEY_ID")
        previous_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
        previous_default_region = os.environ.get("AWS_DEFAULT_REGION")
        previous_region = os.environ.get("AWS_REGION")
        if self.access_key:
            os.environ["AWS_ACCESS_KEY_ID"] = self.access_key
        if self.secret_key:
            os.environ["AWS_SECRET_ACCESS_KEY"] = self.secret_key
        if self.region:
            os.environ["AWS_DEFAULT_REGION"] = self.region
            os.environ["AWS_REGION"] = self.region
        
        try:
            with redirect_stdout(f):
                # We execute the code. The code is expected to print a JSON string at the end.
                exec(code, exec_globals)
            
            output = f.getvalue().strip()
            print(f"  Script Output: {output}")

            # Find the last JSON/Python-dict block in the output
            lines = output.split('\n')
            for line in reversed(lines):
                if line.strip().startswith('{') and line.strip().endswith('}'):
                    try:
                        parsed = json.loads(line)
                        return self._normalize_dynamic_result(parsed, raw_output=output)
                    except Exception:
                        try:
                            parsed = ast.literal_eval(line)
                            if isinstance(parsed, dict):
                                return self._normalize_dynamic_result(parsed, raw_output=output)
                        except Exception:
                            continue

            return {
                "success": False,
                "error": "Dynamic script did not emit a valid JSON result summary.",
                "output": output,
            }
        except Exception as e:
            tb = traceback.format_exc()
            print(f"  Dynamic execution failed: {e}\n{tb}")
            return {"success": False, "error": str(e), "traceback": tb, "output": f.getvalue()}
        finally:
            boto3.client = original_boto3_client
            boto3.resource = original_boto3_resource
            boto3.Session = original_boto3_session
            if previous_access_key is None:
                os.environ.pop("AWS_ACCESS_KEY_ID", None)
            else:
                os.environ["AWS_ACCESS_KEY_ID"] = previous_access_key
            if previous_secret_key is None:
                os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
            else:
                os.environ["AWS_SECRET_ACCESS_KEY"] = previous_secret_key
            if previous_default_region is None:
                os.environ.pop("AWS_DEFAULT_REGION", None)
            else:
                os.environ["AWS_DEFAULT_REGION"] = previous_default_region
            if previous_region is None:
                os.environ.pop("AWS_REGION", None)
            else:
                os.environ["AWS_REGION"] = previous_region

    def _normalize_dynamic_result(
        self,
        parsed: Dict[str, Any],
        raw_output: str = "",
        top_level: bool = True,
    ) -> Dict[str, Any]:
        """Normalize LLM-generated result payloads into a predictable schema."""
        normalized = {}
        key_map = {
            "success": "success",
            "details": "details",
            "error": "error",
            "message": "message",
            "action": "action",
            "resource_type": "resource_type",
            "resource": "resource_type",
        }

        for key, value in parsed.items():
            mapped_key = key_map.get(str(key).strip().lower(), key)
            if isinstance(value, dict):
                normalized[mapped_key] = self._normalize_dynamic_result(
                    value,
                    raw_output="",
                    top_level=False,
                )
            else:
                normalized[mapped_key] = value

        if top_level:
            if "success" in normalized:
                normalized["success"] = bool(normalized["success"])
            else:
                normalized["success"] = False

            if normalized.get("success") is False and not normalized.get("error"):
                details = normalized.get("details")
                if isinstance(details, dict) and details:
                    detail_parts = [f"{k}: {v}" for k, v in details.items()]
                    normalized["error"] = "; ".join(detail_parts[:3])
                elif raw_output:
                    normalized["error"] = "Execution failed. Check output for details."

            if raw_output:
                normalized.setdefault("output", raw_output)

        return normalized

    # ===================== S3 =====================

    async def _create_s3(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        parameters = parameters or {}
        s3 = self._client("s3")
        bucket_name = resource_name or f"ai-platform-{uuid.uuid4().hex[:8]}"
        print(f"  Creating S3 bucket: {bucket_name}")
        public_access = self._to_bool(parameters.get("public_access"), False)
        website_enabled = self._to_bool(
            parameters.get("website_configuration", parameters.get("website_enabled")),
            False,
        )
        index_document = str(parameters.get("index_document", "index.html")).strip() or "index.html"
        error_document = str(parameters.get("error_document", "")).strip()
        index_content = parameters.get("index_content")
        if not isinstance(index_content, str) or not index_content.strip():
            index_content = (
                "<!doctype html><html><head><meta charset=\"utf-8\">"
                "<title>AI Infra Platform</title></head><body><h1>S3 Website Ready</h1>"
                "<p>Bucket provisioned successfully.</p></body></html>"
            )

        create_args = {"Bucket": bucket_name}
        if self.region != "us-east-1":
            create_args["CreateBucketConfiguration"] = {"LocationConstraint": self.region}

        s3.create_bucket(**create_args)

        # Enable versioning if requested or by default for compliance
        versioning_enabled = self._to_bool(parameters.get("versioning", True), True)
        if versioning_enabled:
            s3.put_bucket_versioning(Bucket=bucket_name, VersioningConfiguration={"Status": "Enabled"})

        # Enable encryption
        encryption_enabled = self._to_bool(parameters.get("encryption", True), True)
        if encryption_enabled:
            s3.put_bucket_encryption(
                Bucket=bucket_name,
                ServerSideEncryptionConfiguration={
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
                },
            )

        # Block public access by default, unless explicitly requested.
        if not public_access:
            s3.put_public_access_block(
                Bucket=bucket_name,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )
        else:
            s3.put_public_access_block(
                Bucket=bucket_name,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": False,
                    "IgnorePublicAcls": False,
                    "BlockPublicPolicy": False,
                    "RestrictPublicBuckets": False,
                },
            )

        website_url = None
        if website_enabled:
            website_config = {"IndexDocument": {"Suffix": index_document}}
            if error_document:
                website_config["ErrorDocument"] = {"Key": error_document}
            s3.put_bucket_website(Bucket=bucket_name, WebsiteConfiguration=website_config)

            # Ensure the requested index document exists.
            s3.put_object(
                Bucket=bucket_name,
                Key=index_document,
                Body=index_content.encode("utf-8"),
                ContentType="text/html; charset=utf-8",
            )

            if public_access:
                policy = {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "PublicReadWebsite",
                            "Effect": "Allow",
                            "Principal": "*",
                            "Action": ["s3:GetObject"],
                            "Resource": f"arn:aws:s3:::{bucket_name}/*",
                        }
                    ],
                }
                s3.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(policy))
            website_url = f"http://{bucket_name}.s3-website-{self.region}.amazonaws.com"

        # Tags
        if tags:
            tag_set = [{"Key": k, "Value": v} for k, v in tags.items()]
            s3.put_bucket_tagging(Bucket=bucket_name, Tagging={"TagSet": tag_set})

        return {
            "success": True,
            "action": "create",
            "resource_type": "s3",
            "bucket_name": bucket_name,
            "region": self.region,
            "versioning": "enabled" if versioning_enabled else "disabled",
            "encryption": "AES256" if encryption_enabled else "disabled",
            "public_access": public_access,
            "website_configuration": website_enabled,
            "index_document": index_document if website_enabled else None,
            "website_url": website_url,
            "arn": f"arn:aws:s3:::{bucket_name}",
        }

    async def _delete_s3(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Bucket name is required for deletion. Please specify the bucket name."}
        s3 = self._client("s3")
        print(f"  Deleting S3 bucket: {resource_name}")

        # Empty the bucket first (required before deletion)
        try:
            paginator = s3.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=resource_name):
                objects = []
                for v in page.get("Versions", []):
                    objects.append({"Key": v["Key"], "VersionId": v["VersionId"]})
                for d in page.get("DeleteMarkers", []):
                    objects.append({"Key": d["Key"], "VersionId": d["VersionId"]})
                if objects:
                    s3.delete_objects(Bucket=resource_name, Delete={"Objects": objects})
        except Exception:
            # Try simple object listing if versioning not enabled
            try:
                paginator = s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=resource_name):
                    objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                    if objects:
                        s3.delete_objects(Bucket=resource_name, Delete={"Objects": objects})
            except Exception:
                pass

        s3.delete_bucket(Bucket=resource_name)
        return {"success": True, "action": "delete", "resource_type": "s3", "bucket_name": resource_name, "message": f"Bucket '{resource_name}' deleted successfully"}

    async def _list_s3(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        s3 = self._client("s3")
        response = s3.list_buckets()
        buckets = [{"name": b["Name"], "created": b["CreationDate"].isoformat()} for b in response["Buckets"]]
        return {"success": True, "action": "list", "resource_type": "s3", "buckets": buckets, "count": len(buckets)}

    async def _describe_s3(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Bucket name required for describe"}
        s3 = self._client("s3")
        info = {"bucket_name": resource_name, "region": self.region}
        try:
            v = s3.get_bucket_versioning(Bucket=resource_name)
            info["versioning"] = v.get("Status", "Disabled")
        except Exception:
            pass
        try:
            e = s3.get_bucket_encryption(Bucket=resource_name)
            info["encryption"] = e["ServerSideEncryptionConfiguration"]["Rules"][0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        except Exception:
            info["encryption"] = "None"
        try:
            t = s3.get_bucket_tagging(Bucket=resource_name)
            info["tags"] = {tag["Key"]: tag["Value"] for tag in t["TagSet"]}
        except Exception:
            info["tags"] = {}
        return {"success": True, "action": "describe", "resource_type": "s3", **info}

    async def _update_s3(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Bucket name required for update"}
        parameters = parameters or {}
        s3 = self._client("s3")
        changes = []
        if "versioning" in parameters:
            status = "Enabled" if parameters["versioning"] else "Suspended"
            s3.put_bucket_versioning(Bucket=resource_name, VersioningConfiguration={"Status": status})
            changes.append(f"versioning={status}")
        if "encryption" in parameters:
            if parameters["encryption"]:
                s3.put_bucket_encryption(Bucket=resource_name, ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
                changes.append("encryption=AES256")
        if "website_configuration" in parameters or "website_enabled" in parameters:
            enable_website = self._to_bool(parameters.get("website_configuration", parameters.get("website_enabled")), False)
            if enable_website:
                index_document = str(parameters.get("index_document", "index.html")).strip() or "index.html"
                error_document = str(parameters.get("error_document", "")).strip()
                website_config = {"IndexDocument": {"Suffix": index_document}}
                if error_document:
                    website_config["ErrorDocument"] = {"Key": error_document}
                s3.put_bucket_website(Bucket=resource_name, WebsiteConfiguration=website_config)
                if self._to_bool(parameters.get("create_index_file"), True):
                    index_content = parameters.get("index_content") or "<html><body><h1>Updated by AI Infra Platform</h1></body></html>"
                    s3.put_object(
                        Bucket=resource_name,
                        Key=index_document,
                        Body=str(index_content).encode("utf-8"),
                        ContentType="text/html; charset=utf-8",
                    )
                    changes.append(f"index file upserted ({index_document})")
                changes.append("website configuration enabled")
            else:
                s3.delete_bucket_website(Bucket=resource_name)
                changes.append("website configuration disabled")
        if "public_access" in parameters:
            public_access = self._to_bool(parameters.get("public_access"), False)
            if public_access:
                s3.put_public_access_block(
                    Bucket=resource_name,
                    PublicAccessBlockConfiguration={
                        "BlockPublicAcls": False,
                        "IgnorePublicAcls": False,
                        "BlockPublicPolicy": False,
                        "RestrictPublicBuckets": False,
                    },
                )
                policy = {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "PublicReadWebsite",
                            "Effect": "Allow",
                            "Principal": "*",
                            "Action": ["s3:GetObject"],
                            "Resource": f"arn:aws:s3:::{resource_name}/*",
                        }
                    ],
                }
                s3.put_bucket_policy(Bucket=resource_name, Policy=json.dumps(policy))
                changes.append("public access enabled with bucket policy")
            else:
                s3.put_public_access_block(
                    Bucket=resource_name,
                    PublicAccessBlockConfiguration={
                        "BlockPublicAcls": True,
                        "IgnorePublicAcls": True,
                        "BlockPublicPolicy": True,
                        "RestrictPublicBuckets": True,
                    },
                )
                try:
                    s3.delete_bucket_policy(Bucket=resource_name)
                except Exception:
                    pass
                changes.append("public access blocked")
        if tags:
            tag_set = [{"Key": k, "Value": v} for k, v in tags.items()]
            s3.put_bucket_tagging(Bucket=resource_name, Tagging={"TagSet": tag_set})
            changes.append("tags updated")
        return {"success": True, "action": "update", "resource_type": "s3", "bucket_name": resource_name, "changes": changes}

    # ===================== EC2 =====================

    async def _create_ec2(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        parameters = parameters or {}
        ec2 = self._client("ec2")
        instance_type = parameters.get("instance_type", "t3.micro")
        ami_id = parameters.get("ami_id")
        os_flavor = parameters.get("os_flavor")

        if not ami_id:
            ami_query = self._resolve_ami_query(os_flavor)
            response = ec2.describe_images(**ami_query)
            images = sorted(response["Images"], key=lambda x: x["CreationDate"], reverse=True)
            ami_id = images[0]["ImageId"] if images else "ami-0c55b159cbfafe1f0"

        print(f"  Creating EC2 instance: {instance_type}, name={resource_name}")

        all_tags = {**(tags or {})}
        if resource_name:
            all_tags["Name"] = resource_name

        tag_specs = [{"ResourceType": "instance", "Tags": [{"Key": k, "Value": v} for k, v in all_tags.items()]}]

        run_args = {
            "ImageId": ami_id,
            "InstanceType": instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "TagSpecifications": tag_specs,
            "Monitoring": {"Enabled": True},
        }
        storage_size = parameters.get("storage_size")
        if isinstance(storage_size, str) and storage_size.isdigit():
            storage_size = int(storage_size)
        if isinstance(storage_size, (int, float)) and int(storage_size) > 0:
            device_name = "/dev/sda1" if (os_flavor or "").lower().startswith("windows") else "/dev/xvda"
            run_args["BlockDeviceMappings"] = [{
                "DeviceName": device_name,
                "Ebs": {
                    "VolumeSize": int(storage_size),
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True,
                },
            }]
        key_name = parameters.get("key_name")
        if isinstance(key_name, str) and key_name.strip().lower() not in {"", "null", "none", "default"}:
            run_args["KeyName"] = key_name.strip()
        elif key_name and not isinstance(key_name, str):
            run_args["KeyName"] = key_name

        user_data = parameters.get("user_data")
        if isinstance(user_data, str) and user_data.strip():
            run_args["UserData"] = self._sanitize_user_data_script(user_data)

        created_sg_id = None
        requested_sg_ids = parameters.get("vpc_security_group_ids") or parameters.get("security_group_ids")
        if isinstance(requested_sg_ids, str):
            requested_sg_ids = [sg.strip() for sg in requested_sg_ids.split(",") if sg.strip()]
        if isinstance(requested_sg_ids, list):
            requested_sg_ids = [sg for sg in requested_sg_ids if isinstance(sg, str) and sg.strip().startswith("sg-")]
        else:
            requested_sg_ids = []

        requested_port = parameters.get("port")
        wants_public_access = bool(parameters.get("public_access"))

        if parameters.get("security_group_id"):
            created_sg_id = parameters["security_group_id"]
        elif requested_sg_ids:
            run_args["SecurityGroupIds"] = requested_sg_ids
        elif requested_port or wants_public_access or parameters.get("cidr_block"):
            sg_name_base = resource_name or "ai-ec2"
            sg_name = f"{sg_name_base[:40]}-sg-{uuid.uuid4().hex[:6]}"
            sg_result = await self._create_security_group(
                resource_name=sg_name,
                parameters={
                    "description": f"Security group for EC2 instance '{resource_name or sg_name_base}'",
                    "vpc_id": parameters.get("vpc_id"),
                    "port": requested_port,
                    "protocol": parameters.get("protocol", "tcp"),
                    "cidr_block": parameters.get("cidr_block", "0.0.0.0/0" if wants_public_access else None),
                },
                tags=tags,
            )
            if not sg_result.get("success"):
                return {
                    "success": False,
                    "error": sg_result.get("error", "Failed to create security group for EC2 instance"),
                    "details": {"security_group": sg_result},
                }
            created_sg_id = sg_result.get("group_id")

        if created_sg_id:
            run_args["SecurityGroupIds"] = [created_sg_id]

        if wants_public_access:
            subnet_id = parameters.get("subnet_id")
            if not subnet_id:
                try:
                    subnets = ec2.describe_subnets(
                        Filters=[{"Name": "default-for-az", "Values": ["true"]}]
                    ).get("Subnets", [])
                    if subnets:
                        subnet_id = subnets[0]["SubnetId"]
                except Exception:
                    subnet_id = None

            if subnet_id:
                nic = {
                    "DeviceIndex": 0,
                    "SubnetId": subnet_id,
                    "AssociatePublicIpAddress": True,
                }
                if created_sg_id:
                    nic["Groups"] = [created_sg_id]
                run_args["NetworkInterfaces"] = [nic]
                run_args.pop("SecurityGroupIds", None)

        response = ec2.run_instances(**run_args)
        instance = response["Instances"][0]
        instance_id = instance["InstanceId"]
        should_wait = bool(parameters.get("wait_for_instance")) or bool(parameters.get("install_targets"))
        readiness = "launch_submitted"

        if should_wait:
            try:
                ec2.get_waiter("instance_running").wait(
                    InstanceIds=[instance_id],
                    WaiterConfig={"Delay": 10, "MaxAttempts": 30},
                )
                ec2.get_waiter("instance_status_ok").wait(
                    InstanceIds=[instance_id],
                    WaiterConfig={"Delay": 15, "MaxAttempts": 20},
                )
                refreshed = ec2.describe_instances(InstanceIds=[instance_id])
                reservations = refreshed.get("Reservations", [])
                if reservations and reservations[0].get("Instances"):
                    instance = reservations[0]["Instances"][0]
                readiness = "instance_status_ok"
            except Exception as e:
                readiness = f"checks_pending: {e}"

        return {
            "success": True,
            "action": "create",
            "resource_type": "ec2",
            "instance_id": instance_id,
            "instance_type": instance_type,
            "ami_id": ami_id,
            "os_flavor": os_flavor or "amazon-linux-2",
            "storage_size": int(storage_size) if isinstance(storage_size, (int, float)) else None,
            "name": resource_name,
            "state": instance["State"]["Name"],
            "region": self.region,
            "security_group_id": created_sg_id,
            "public_access": wants_public_access,
            "port": requested_port,
            "public_ip": instance.get("PublicIpAddress"),
            "private_ip": instance.get("PrivateIpAddress"),
            "readiness": readiness,
            "installation_mode": "cloud-init user_data" if run_args.get("UserData") else "none",
        }

    async def _delete_ec2(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        instance_id = parameters.get("instance_id") or resource_name

        if not instance_id:
            return {"success": False, "error": "Instance ID or name is required for termination"}

        # If resource_name looks like a name (not i-xxx), find instance by Name tag
        if instance_id and not instance_id.startswith("i-"):
            response = ec2.describe_instances(Filters=[{"Name": "tag:Name", "Values": [instance_id]}])
            instances = []
            for r in response["Reservations"]:
                for inst in r["Instances"]:
                    if inst["State"]["Name"] not in ("terminated", "shutting-down"):
                        instances.append(inst["InstanceId"])
            if not instances:
                return {"success": False, "error": f"No running instance found with name '{instance_id}'"}
            instance_id = instances[0]

        ec2.terminate_instances(InstanceIds=[instance_id])
        return {"success": True, "action": "delete", "resource_type": "ec2", "instance_id": instance_id, "message": f"Instance {instance_id} terminated"}

    async def _list_ec2(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        response = ec2.describe_instances()
        instances = []
        for r in response["Reservations"]:
            for inst in r["Instances"]:
                name = ""
                for tag in inst.get("Tags", []):
                    if tag["Key"] == "Name":
                        name = tag["Value"]
                instances.append({
                    "instance_id": inst["InstanceId"],
                    "name": name,
                    "type": inst["InstanceType"],
                    "state": inst["State"]["Name"],
                    "launch_time": inst.get("LaunchTime", "").isoformat() if inst.get("LaunchTime") else "",
                })
        return {"success": True, "action": "list", "resource_type": "ec2", "instances": instances, "count": len(instances)}

    async def _describe_ec2(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        instance_id = parameters.get("instance_id") or resource_name
        if not instance_id:
            return {"success": False, "error": "Instance ID or name required"}
        filters = []
        if instance_id.startswith("i-"):
            response = ec2.describe_instances(InstanceIds=[instance_id])
        else:
            response = ec2.describe_instances(Filters=[{"Name": "tag:Name", "Values": [instance_id]}])
        instances = []
        for r in response["Reservations"]:
            instances.extend(r["Instances"])
        if not instances:
            return {"success": False, "error": f"Instance '{instance_id}' not found"}
        inst = instances[0]
        return {
            "success": True, "action": "describe", "resource_type": "ec2",
            "instance_id": inst["InstanceId"], "type": inst["InstanceType"],
            "state": inst["State"]["Name"], "ami": inst.get("ImageId"),
            "public_ip": inst.get("PublicIpAddress"), "private_ip": inst.get("PrivateIpAddress"),
            "tags": {t["Key"]: t["Value"] for t in inst.get("Tags", [])},
        }

    async def _update_ec2(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        instance_id = parameters.get("instance_id") or resource_name
        if not instance_id:
            return {"success": False, "error": "Instance ID required for update"}
        if not instance_id.startswith("i-"):
            response = ec2.describe_instances(Filters=[{"Name": "tag:Name", "Values": [instance_id]}])
            for r in response["Reservations"]:
                for inst in r["Instances"]:
                    if inst["State"]["Name"] not in ("terminated",):
                        instance_id = inst["InstanceId"]
                        break
        changes = []
        if parameters.get("instance_type"):
            ec2.modify_instance_attribute(InstanceId=instance_id, InstanceType={"Value": parameters["instance_type"]})
            changes.append(f"instance_type={parameters['instance_type']}")
        if tags:
            ec2.create_tags(Resources=[instance_id], Tags=[{"Key": k, "Value": v} for k, v in tags.items()])
            changes.append("tags updated")
        return {"success": True, "action": "update", "resource_type": "ec2", "instance_id": instance_id, "changes": changes}

    # ===================== RDS =====================

    async def _create_rds(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        rds = self._client("rds")
        parameters = parameters or {}
        db_id = (
            parameters.get("db_instance_id")
            or parameters.get("DBInstanceIdentifier")
            or resource_name
            or f"ai-db-{uuid.uuid4().hex[:8]}"
        )
        engine = parameters.get("engine", "mysql")
        instance_class = parameters.get("instance_type") or parameters.get("instance_class") or parameters.get("DBInstanceClass") or "db.t3.micro"
        storage = parameters.get("storage_size", 20)
        try:
            storage = int(storage)
        except Exception:
            storage = 20
        master_username = parameters.get("master_username", "dbadmin")
        master_password = parameters.get("master_password", f"AutoPass-{uuid.uuid4().hex[:12]}")

        print(f"  Creating RDS instance: {db_id}, engine={engine}")

        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]

        create_kwargs = {
            "DBInstanceIdentifier": db_id,
            "DBInstanceClass": instance_class,
            "Engine": engine,
            "AllocatedStorage": storage,
            "MasterUsername": master_username,
            "MasterUserPassword": master_password,
            "StorageEncrypted": parameters.get("encryption", True),
            "Tags": tag_list,
        }
        class_candidates = [instance_class]
        # Free-plan/freetier accounts can reject some classes; try common alternatives.
        if instance_class != "db.t4g.micro":
            class_candidates.append("db.t4g.micro")
        if instance_class != "db.t3.micro":
            class_candidates.append("db.t3.micro")

        last_error = None
        response = None
        for candidate in class_candidates:
            create_kwargs["DBInstanceClass"] = candidate
            try:
                response = rds.create_db_instance(**create_kwargs)
                instance_class = candidate
                last_error = None
                break
            except Exception as e:
                last_error = e
                msg = str(e)
                if "DBInstanceAlreadyExists" in msg:
                    existing = rds.describe_db_instances(DBInstanceIdentifier=db_id)["DBInstances"][0]
                    return {
                        "success": True,
                        "action": "create",
                        "resource_type": "rds",
                        "db_instance_id": existing["DBInstanceIdentifier"],
                        "engine": existing["Engine"],
                        "instance_class": existing["DBInstanceClass"],
                        "storage_gb": existing["AllocatedStorage"],
                        "status": existing["DBInstanceStatus"],
                        "arn": existing["DBInstanceArn"],
                        "reconciled_existing": True,
                    }
                if "FreeTierRestrictionError" in msg and candidate != class_candidates[-1]:
                    continue
                if "MasterUsername" in msg and "reserved word" in msg.lower():
                    # Retry once with a safer username.
                    create_kwargs["MasterUsername"] = "appadmin"
                    try:
                        response = rds.create_db_instance(**create_kwargs)
                        instance_class = candidate
                        last_error = None
                        break
                    except Exception as inner:
                        last_error = inner
                # Otherwise stop after this candidate unless it was free-tier retry path.
                if "FreeTierRestrictionError" not in msg:
                    break

        if response is None and last_error is not None:
            raise last_error

        db = response["DBInstance"]
        return {
            "success": True, "action": "create", "resource_type": "rds",
            "db_instance_id": db["DBInstanceIdentifier"], "engine": engine,
            "instance_class": instance_class, "storage_gb": storage,
            "status": db["DBInstanceStatus"], "arn": db["DBInstanceArn"],
        }

    async def _delete_rds(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "DB instance identifier required for deletion"}
        rds = self._client("rds")
        rds.delete_db_instance(DBInstanceIdentifier=resource_name, SkipFinalSnapshot=True)
        return {"success": True, "action": "delete", "resource_type": "rds", "db_instance_id": resource_name, "message": f"RDS instance '{resource_name}' deletion initiated"}

    async def _list_rds(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        rds = self._client("rds")
        response = rds.describe_db_instances()
        dbs = [{"id": d["DBInstanceIdentifier"], "engine": d["Engine"], "status": d["DBInstanceStatus"], "class": d["DBInstanceClass"]} for d in response["DBInstances"]]
        return {"success": True, "action": "list", "resource_type": "rds", "databases": dbs, "count": len(dbs)}

    async def _describe_rds(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "DB instance identifier required"}
        rds = self._client("rds")
        response = rds.describe_db_instances(DBInstanceIdentifier=resource_name)
        db = response["DBInstances"][0]
        return {
            "success": True, "action": "describe", "resource_type": "rds",
            "db_instance_id": db["DBInstanceIdentifier"], "engine": db["Engine"],
            "status": db["DBInstanceStatus"], "class": db["DBInstanceClass"],
            "storage_gb": db["AllocatedStorage"], "endpoint": db.get("Endpoint", {}).get("Address"),
            "port": db.get("Endpoint", {}).get("Port"), "arn": db["DBInstanceArn"],
        }

    async def _update_rds(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        rds = self._client("rds")
        parameters = parameters or {}
        db_id = (
            parameters.get("db_instance_id")
            or parameters.get("DBInstanceIdentifier")
            or resource_name
        )
        if not db_id:
            return {"success": False, "error": "DB instance identifier required for update"}

        changes: List[str] = []
        modify_args: Dict[str, Any] = {"DBInstanceIdentifier": db_id}
        if parameters.get("instance_type") or parameters.get("instance_class"):
            modify_args["DBInstanceClass"] = parameters.get("instance_type") or parameters.get("instance_class")
            changes.append(f"instance_class={modify_args['DBInstanceClass']}")
        if parameters.get("storage_size") not in (None, "", []):
            try:
                modify_args["AllocatedStorage"] = int(parameters.get("storage_size"))
                changes.append(f"storage_gb={modify_args['AllocatedStorage']}")
            except Exception:
                return {"success": False, "error": "storage_size must be a valid integer"}
        if parameters.get("master_password"):
            modify_args["MasterUserPassword"] = str(parameters.get("master_password"))
            changes.append("master_password=updated")
        if len(modify_args) > 1:
            modify_args["ApplyImmediately"] = self._to_bool(parameters.get("apply_immediately"), default=True)
            rds.modify_db_instance(**modify_args)

        db = rds.describe_db_instances(DBInstanceIdentifier=db_id)["DBInstances"][0]
        db_arn = db.get("DBInstanceArn")
        if db_arn and tags:
            tag_list = [{"Key": str(k), "Value": str(v)} for k, v in tags.items()]
            if tag_list:
                try:
                    rds.add_tags_to_resource(ResourceName=db_arn, Tags=tag_list)
                    changes.append("tags updated")
                except Exception:
                    pass

        if not changes:
            changes.append("no mutable fields provided; current configuration retained")
        return {
            "success": True,
            "action": "update",
            "resource_type": "rds",
            "db_instance_id": db_id,
            "status": db.get("DBInstanceStatus"),
            "endpoint": (db.get("Endpoint") or {}).get("Address"),
            "port": (db.get("Endpoint") or {}).get("Port"),
            "changes": changes,
        }

    # ===================== Lambda =====================

    async def _create_lambda(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        lam = self._client("lambda")
        func_name = resource_name or f"ai-func-{uuid.uuid4().hex[:8]}"
        runtime = parameters.get("runtime", "python3.12")
        role = parameters.get("role_arn", "")

        # If no role, create a basic execution role
        if not role:
            iam = self._client("iam")
            role_name = f"{func_name}-role"
            trust_policy = json.dumps({
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
            })
            try:
                role_resp = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=trust_policy, Tags=[{"Key": k, "Value": v} for k, v in (tags or {}).items()])
                role = role_resp["Role"]["Arn"]
                iam.attach_role_policy(RoleName=role_name, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
                import time; time.sleep(10)  # Wait for IAM propagation
            except Exception as e:
                return {"success": False, "error": f"Failed to create execution role: {e}"}

        # Default handler code
        code = parameters.get("code", "def handler(event, context):\n    return {'statusCode': 200, 'body': 'Hello from AI Platform'}")
        import zipfile, io
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("lambda_function.py", code)
        zip_buffer.seek(0)

        response = lam.create_function(
            FunctionName=func_name,
            Runtime=runtime,
            Role=role,
            Handler="lambda_function.handler",
            Code={"ZipFile": zip_buffer.read()},
            Timeout=parameters.get("timeout", 30),
            MemorySize=parameters.get("memory", 128),
            Tags=tags or {},
        )

        return {
            "success": True, "action": "create", "resource_type": "lambda",
            "function_name": response["FunctionName"], "arn": response["FunctionArn"],
            "runtime": runtime, "memory": parameters.get("memory", 128),
            "timeout": parameters.get("timeout", 30),
        }

    async def _delete_lambda(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Function name required for deletion"}
        lam = self._client("lambda")
        lam.delete_function(FunctionName=resource_name)
        return {"success": True, "action": "delete", "resource_type": "lambda", "function_name": resource_name, "message": f"Lambda function '{resource_name}' deleted"}

    async def _list_lambda(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        lam = self._client("lambda")
        response = lam.list_functions()
        functions = [{"name": f["FunctionName"], "runtime": f.get("Runtime"), "memory": f["MemorySize"], "last_modified": f["LastModified"]} for f in response["Functions"]]
        return {"success": True, "action": "list", "resource_type": "lambda", "functions": functions, "count": len(functions)}

    async def _describe_lambda(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Function name required"}
        lam = self._client("lambda")
        f = lam.get_function(FunctionName=resource_name)
        config = f["Configuration"]
        return {
            "success": True, "action": "describe", "resource_type": "lambda",
            "function_name": config["FunctionName"], "arn": config["FunctionArn"],
            "runtime": config.get("Runtime"), "memory": config["MemorySize"],
            "timeout": config["Timeout"], "state": config.get("State"),
        }

    async def _update_lambda(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        lam = self._client("lambda")
        parameters = parameters or {}
        function_name = (
            parameters.get("function_name")
            or parameters.get("FunctionName")
            or resource_name
        )
        if not function_name:
            return {"success": False, "error": "Function name required for update"}

        changes: List[str] = []
        config_args: Dict[str, Any] = {"FunctionName": function_name}
        if parameters.get("runtime"):
            config_args["Runtime"] = str(parameters.get("runtime"))
            changes.append(f"runtime={config_args['Runtime']}")
        if parameters.get("timeout") not in (None, "", []):
            config_args["Timeout"] = int(parameters.get("timeout"))
            changes.append(f"timeout={config_args['Timeout']}")
        if parameters.get("memory") not in (None, "", []):
            config_args["MemorySize"] = int(parameters.get("memory"))
            changes.append(f"memory={config_args['MemorySize']}")
        if parameters.get("role_arn"):
            config_args["Role"] = str(parameters.get("role_arn"))
            changes.append("role_arn=updated")
        if parameters.get("handler"):
            config_args["Handler"] = str(parameters.get("handler"))
            changes.append(f"handler={config_args['Handler']}")
        if len(config_args) > 1:
            lam.update_function_configuration(**config_args)

        code_value = parameters.get("code")
        if code_value not in (None, "", []):
            import zipfile
            import io

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w") as zf:
                zf.writestr("lambda_function.py", str(code_value))
            zip_buffer.seek(0)
            lam.update_function_code(
                FunctionName=function_name,
                ZipFile=zip_buffer.read(),
                Publish=bool(self._to_bool(parameters.get("publish"), default=False)),
            )
            changes.append("function code updated")

        if tags:
            try:
                describe = lam.get_function(FunctionName=function_name)
                function_arn = (describe.get("Configuration") or {}).get("FunctionArn")
                if function_arn:
                    lam.tag_resource(Resource=function_arn, Tags={str(k): str(v) for k, v in tags.items()})
                    changes.append("tags updated")
            except Exception:
                pass

        describe = lam.get_function(FunctionName=function_name)
        config = describe.get("Configuration", {})
        if not changes:
            changes.append("no mutable fields provided; current configuration retained")
        return {
            "success": True,
            "action": "update",
            "resource_type": "lambda",
            "function_name": config.get("FunctionName", function_name),
            "arn": config.get("FunctionArn"),
            "runtime": config.get("Runtime"),
            "memory": config.get("MemorySize"),
            "timeout": config.get("Timeout"),
            "state": config.get("State"),
            "changes": changes,
        }

    # ===================== VPC =====================

    async def _create_vpc(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        cidr = parameters.get("cidr_block", "10.0.0.0/16")

        all_tags = {**(tags or {})}
        if resource_name:
            all_tags["Name"] = resource_name

        response = ec2.create_vpc(CidrBlock=cidr, TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": k, "Value": v} for k, v in all_tags.items()]}])
        vpc = response["Vpc"]
        ec2.modify_vpc_attribute(VpcId=vpc["VpcId"], EnableDnsHostnames={"Value": True})
        ec2.modify_vpc_attribute(VpcId=vpc["VpcId"], EnableDnsSupport={"Value": True})

        return {
            "success": True, "action": "create", "resource_type": "vpc",
            "vpc_id": vpc["VpcId"], "cidr_block": cidr, "name": resource_name,
            "state": vpc["State"],
        }

    async def _delete_vpc(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        vpc_id = parameters.get("vpc_id") or resource_name
        if not vpc_id:
            return {"success": False, "error": "VPC ID required for deletion"}
        if not vpc_id.startswith("vpc-"):
            # Find by name tag
            resp = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [vpc_id]}])
            if resp["Vpcs"]:
                vpc_id = resp["Vpcs"][0]["VpcId"]
            else:
                return {"success": False, "error": f"VPC with name '{vpc_id}' not found"}
        ec2.delete_vpc(VpcId=vpc_id)
        return {"success": True, "action": "delete", "resource_type": "vpc", "vpc_id": vpc_id, "message": f"VPC {vpc_id} deleted"}

    async def _list_vpc(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        response = ec2.describe_vpcs()
        vpcs = []
        for v in response["Vpcs"]:
            name = ""
            for tag in v.get("Tags", []):
                if tag["Key"] == "Name":
                    name = tag["Value"]
            vpcs.append({"vpc_id": v["VpcId"], "name": name, "cidr": v["CidrBlock"], "state": v["State"]})
        return {"success": True, "action": "list", "resource_type": "vpc", "vpcs": vpcs, "count": len(vpcs)}

    # ===================== DynamoDB =====================

    async def _create_dynamodb(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ddb = self._client("dynamodb")
        table_name = resource_name or f"ai-table-{uuid.uuid4().hex[:8]}"
        partition_key = parameters.get("partition_key", "id")
        sort_key = parameters.get("sort_key")

        key_schema = [{"AttributeName": partition_key, "KeyType": "HASH"}]
        attr_defs = [{"AttributeName": partition_key, "AttributeType": "S"}]
        if sort_key:
            key_schema.append({"AttributeName": sort_key, "KeyType": "RANGE"})
            attr_defs.append({"AttributeName": sort_key, "AttributeType": "S"})

        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]

        response = ddb.create_table(
            TableName=table_name,
            KeySchema=key_schema,
            AttributeDefinitions=attr_defs,
            BillingMode=parameters.get("billing_mode", "PAY_PER_REQUEST"),
            Tags=tag_list,
        )
        table = response["TableDescription"]
        return {
            "success": True, "action": "create", "resource_type": "dynamodb",
            "table_name": table["TableName"], "arn": table["TableArn"],
            "status": table["TableStatus"], "partition_key": partition_key,
        }

    async def _delete_dynamodb(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Table name required for deletion"}
        ddb = self._client("dynamodb")
        ddb.delete_table(TableName=resource_name)
        return {"success": True, "action": "delete", "resource_type": "dynamodb", "table_name": resource_name, "message": f"Table '{resource_name}' deletion initiated"}

    async def _list_dynamodb(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ddb = self._client("dynamodb")
        response = ddb.list_tables()
        return {"success": True, "action": "list", "resource_type": "dynamodb", "tables": response["TableNames"], "count": len(response["TableNames"])}

    async def _describe_dynamodb(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Table name required"}
        ddb = self._client("dynamodb")
        response = ddb.describe_table(TableName=resource_name)
        t = response["Table"]
        return {
            "success": True, "action": "describe", "resource_type": "dynamodb",
            "table_name": t["TableName"], "status": t["TableStatus"],
            "item_count": t.get("ItemCount", 0), "size_bytes": t.get("TableSizeBytes", 0),
            "arn": t["TableArn"],
        }

    # ===================== SNS =====================

    async def _create_sns(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        sns = self._client("sns")
        topic_name = resource_name or f"ai-topic-{uuid.uuid4().hex[:8]}"
        attrs = {}
        if parameters.get("encryption"):
            attrs["KmsMasterKeyId"] = "alias/aws/sns"

        response = sns.create_topic(Name=topic_name, Attributes=attrs, Tags=[{"Key": k, "Value": v} for k, v in (tags or {}).items()])
        return {"success": True, "action": "create", "resource_type": "sns", "topic_name": topic_name, "arn": response["TopicArn"]}

    async def _delete_sns(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Topic ARN or name required for deletion"}
        sns = self._client("sns")
        # If not an ARN, find it
        topic_arn = resource_name
        if not resource_name.startswith("arn:"):
            topics = sns.list_topics()
            for t in topics["Topics"]:
                if t["TopicArn"].split(":")[-1] == resource_name:
                    topic_arn = t["TopicArn"]
                    break
        sns.delete_topic(TopicArn=topic_arn)
        return {"success": True, "action": "delete", "resource_type": "sns", "topic": resource_name, "message": f"Topic '{resource_name}' deleted"}

    async def _list_sns(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        sns = self._client("sns")
        response = sns.list_topics()
        topics = [{"arn": t["TopicArn"], "name": t["TopicArn"].split(":")[-1]} for t in response["Topics"]]
        return {"success": True, "action": "list", "resource_type": "sns", "topics": topics, "count": len(topics)}

    # ===================== SQS =====================

    async def _create_sqs(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        sqs = self._client("sqs")
        queue_name = resource_name or f"ai-queue-{uuid.uuid4().hex[:8]}"
        attrs = {}
        if parameters.get("delay_seconds"):
            attrs["DelaySeconds"] = str(parameters["delay_seconds"])
        if parameters.get("visibility_timeout"):
            attrs["VisibilityTimeout"] = str(parameters["visibility_timeout"])

        response = sqs.create_queue(QueueName=queue_name, Attributes=attrs, tags=tags or {})
        return {"success": True, "action": "create", "resource_type": "sqs", "queue_name": queue_name, "queue_url": response["QueueUrl"]}

    async def _delete_sqs(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Queue name or URL required for deletion"}
        sqs = self._client("sqs")
        queue_url = resource_name
        if not resource_name.startswith("https://"):
            response = sqs.get_queue_url(QueueName=resource_name)
            queue_url = response["QueueUrl"]
        sqs.delete_queue(QueueUrl=queue_url)
        return {"success": True, "action": "delete", "resource_type": "sqs", "queue": resource_name, "message": f"Queue '{resource_name}' deleted"}

    async def _list_sqs(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        sqs = self._client("sqs")
        response = sqs.list_queues()
        urls = response.get("QueueUrls", [])
        queues = [{"url": u, "name": u.split("/")[-1]} for u in urls]
        return {"success": True, "action": "list", "resource_type": "sqs", "queues": queues, "count": len(queues)}

    # ===================== IAM =====================

    async def _create_iam(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        iam = self._client("iam")
        # Determine if creating user, role, or policy
        iam_type = parameters.get("iam_type", "role")
        name = resource_name or f"ai-{iam_type}-{uuid.uuid4().hex[:8]}"
        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]

        if iam_type == "user":
            response = iam.create_user(UserName=name, Tags=tag_list)
            return {"success": True, "action": "create", "resource_type": "iam", "iam_type": "user", "user_name": name, "arn": response["User"]["Arn"]}
        elif iam_type == "policy":
            policy_doc = parameters.get("policy_document", json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}))
            response = iam.create_policy(PolicyName=name, PolicyDocument=policy_doc, Tags=tag_list)
            return {"success": True, "action": "create", "resource_type": "iam", "iam_type": "policy", "policy_name": name, "arn": response["Policy"]["Arn"]}
        else:
            trust_policy = parameters.get("trust_policy", json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}))
            response = iam.create_role(RoleName=name, AssumeRolePolicyDocument=trust_policy, Tags=tag_list)
            return {"success": True, "action": "create", "resource_type": "iam", "iam_type": "role", "role_name": name, "arn": response["Role"]["Arn"]}

    async def _delete_iam(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "IAM resource name required for deletion"}
        iam = self._client("iam")
        iam_type = parameters.get("iam_type", "role")
        if iam_type == "user":
            iam.delete_user(UserName=resource_name)
        elif iam_type == "policy":
            # Need ARN for policy deletion
            return {"success": False, "error": "Policy ARN needed for deletion. Use 'describe' to find it."}
        else:
            # Detach all policies first
            try:
                policies = iam.list_attached_role_policies(RoleName=resource_name)
                for p in policies["AttachedPolicies"]:
                    iam.detach_role_policy(RoleName=resource_name, PolicyArn=p["PolicyArn"])
            except Exception:
                pass
            iam.delete_role(RoleName=resource_name)
        return {"success": True, "action": "delete", "resource_type": "iam", "name": resource_name, "message": f"IAM {iam_type} '{resource_name}' deleted"}

    async def _list_iam(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        iam = self._client("iam")
        roles = iam.list_roles()["Roles"]
        items = [{"name": r["RoleName"], "arn": r["Arn"], "type": "role"} for r in roles[:20]]
        return {"success": True, "action": "list", "resource_type": "iam", "items": items, "count": len(items)}

    # ===================== Security Groups =====================

    async def _create_security_group(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        parameters = parameters or {}
        tags = tags or {}
        ec2 = self._client("ec2")
        sg_name = resource_name or f"ai-sg-{uuid.uuid4().hex[:8]}"
        description = self._sanitize_security_group_description(
            parameters.get("description", "Security group created by AI Platform")
        )
        vpc_id = parameters.get("vpc_id")
        if isinstance(vpc_id, str) and vpc_id.strip().lower() in {"", "null", "none", "default"}:
            vpc_id = self._resolve_default_vpc_id(ec2)

        create_args = {"GroupName": sg_name, "Description": description}
        if vpc_id:
            create_args["VpcId"] = vpc_id

        all_tags = {**tags, "Name": sg_name}
        create_args["TagSpecifications"] = [{"ResourceType": "security-group", "Tags": [{"Key": k, "Value": v} for k, v in all_tags.items()]}]

        response = ec2.create_security_group(**create_args)
        sg_id = response["GroupId"]

        # Add ingress rules if specified
        port = parameters.get("port")
        if port:
            cidr_block = parameters.get("cidr_block") or "0.0.0.0/0"
            ip_ranges = [{"CidrIp": cidr_block}]
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{
                    "IpProtocol": parameters.get("protocol", "tcp"),
                    "FromPort": port, "ToPort": port,
                    "IpRanges": ip_ranges,
                }],
            )

        return {"success": True, "action": "create", "resource_type": "security_group", "group_id": sg_id, "group_name": sg_name}

    async def _delete_security_group(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        sg_id = parameters.get("group_id") or resource_name
        if not sg_id:
            return {"success": False, "error": "Security group ID or name required"}
        if not sg_id.startswith("sg-"):
            resp = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [sg_id]}])
            if resp["SecurityGroups"]:
                sg_id = resp["SecurityGroups"][0]["GroupId"]
        ec2.delete_security_group(GroupId=sg_id)
        return {"success": True, "action": "delete", "resource_type": "security_group", "group_id": sg_id, "message": f"Security group {sg_id} deleted"}

    async def _list_security_group(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        response = ec2.describe_security_groups()
        sgs = [{"id": sg["GroupId"], "name": sg["GroupName"], "vpc_id": sg.get("VpcId", "")} for sg in response["SecurityGroups"]]
        return {"success": True, "action": "list", "resource_type": "security_group", "security_groups": sgs, "count": len(sgs)}

    # ===================== EBS =====================

    async def _create_ebs(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        size = parameters.get("storage_size", 20)
        vol_type = parameters.get("volume_type", "gp3")
        az = parameters.get("availability_zone", f"{self.region}a")

        all_tags = {**(tags or {})}
        if resource_name:
            all_tags["Name"] = resource_name

        response = ec2.create_volume(
            Size=size, VolumeType=vol_type, AvailabilityZone=az, Encrypted=parameters.get("encryption", True),
            TagSpecifications=[{"ResourceType": "volume", "Tags": [{"Key": k, "Value": v} for k, v in all_tags.items()]}],
        )
        return {
            "success": True, "action": "create", "resource_type": "ebs",
            "volume_id": response["VolumeId"], "size_gb": size, "type": vol_type,
            "availability_zone": az, "encrypted": True, "name": resource_name,
        }

    async def _delete_ebs(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        vol_id = parameters.get("volume_id") or resource_name
        if not vol_id:
            return {"success": False, "error": "Volume ID required"}
        if not vol_id.startswith("vol-"):
            resp = ec2.describe_volumes(Filters=[{"Name": "tag:Name", "Values": [vol_id]}])
            if resp["Volumes"]:
                vol_id = resp["Volumes"][0]["VolumeId"]
        ec2.delete_volume(VolumeId=vol_id)
        return {"success": True, "action": "delete", "resource_type": "ebs", "volume_id": vol_id}

    async def _list_ebs(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        response = ec2.describe_volumes()
        vols = []
        for v in response["Volumes"]:
            name = ""
            for t in v.get("Tags", []):
                if t["Key"] == "Name":
                    name = t["Value"]
            vols.append({"volume_id": v["VolumeId"], "name": name, "size_gb": v["Size"], "state": v["State"], "type": v["VolumeType"]})
        return {"success": True, "action": "list", "resource_type": "ebs", "volumes": vols, "count": len(vols)}

    # ===================== CloudWatch =====================

    async def _create_cloudwatch(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        cw = self._client("cloudwatch")
        alarm_name = resource_name or f"ai-alarm-{uuid.uuid4().hex[:8]}"
        metric = parameters.get("metric_name", "CPUUtilization")
        namespace = parameters.get("namespace", "AWS/EC2")
        threshold = parameters.get("threshold", 80.0)
        period = parameters.get("period", 300)

        cw.put_metric_alarm(
            AlarmName=alarm_name,
            MetricName=metric,
            Namespace=namespace,
            Statistic="Average",
            Period=period,
            EvaluationPeriods=2,
            Threshold=threshold,
            ComparisonOperator="GreaterThanThreshold",
            Tags=[{"Key": k, "Value": v} for k, v in (tags or {}).items()],
        )
        return {"success": True, "action": "create", "resource_type": "cloudwatch", "alarm_name": alarm_name, "metric": metric, "threshold": threshold}

    async def _delete_cloudwatch(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Alarm name required"}
        cw = self._client("cloudwatch")
        cw.delete_alarms(AlarmNames=[resource_name])
        return {"success": True, "action": "delete", "resource_type": "cloudwatch", "alarm_name": resource_name, "message": f"Alarm '{resource_name}' deleted"}

    async def _list_cloudwatch(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        cw = self._client("cloudwatch")
        response = cw.describe_alarms()
        alarms = [{"name": a["AlarmName"], "state": a["StateValue"], "metric": a["MetricName"]} for a in response["MetricAlarms"]]
        return {"success": True, "action": "list", "resource_type": "cloudwatch", "alarms": alarms, "count": len(alarms)}

    # ===================== ECS =====================

    async def _create_ecs(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ecs = self._client("ecs")
        cluster_name = resource_name or f"ai-cluster-{uuid.uuid4().hex[:8]}"
        tag_list = [{"key": k, "value": v} for k, v in (tags or {}).items()]
        response = ecs.create_cluster(clusterName=cluster_name, tags=tag_list)
        cluster = response["cluster"]
        return {"success": True, "action": "create", "resource_type": "ecs", "cluster_name": cluster["clusterName"], "arn": cluster["clusterArn"], "status": cluster["status"]}

    async def _delete_ecs(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Cluster name required"}
        ecs = self._client("ecs")
        ecs.delete_cluster(cluster=resource_name)
        return {"success": True, "action": "delete", "resource_type": "ecs", "cluster_name": resource_name, "message": f"ECS cluster '{resource_name}' deleted"}

    async def _list_ecs(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ecs = self._client("ecs")
        arns = ecs.list_clusters()["clusterArns"]
        clusters = []
        if arns:
            details = ecs.describe_clusters(clusters=arns)
            clusters = [{"name": c["clusterName"], "arn": c["clusterArn"], "status": c["status"]} for c in details["clusters"]]
        return {"success": True, "action": "list", "resource_type": "ecs", "clusters": clusters, "count": len(clusters)}

    # ===================== Secrets Manager =====================

    async def _create_secretsmanager(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        sm = self._client("secretsmanager")
        secret_name = resource_name or f"ai-secret-{uuid.uuid4().hex[:8]}"
        secret_value = parameters.get("secret_value", parameters.get("value", ""))
        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]
        response = sm.create_secret(Name=secret_name, SecretString=secret_value or "{}", Tags=tag_list)
        return {"success": True, "action": "create", "resource_type": "secretsmanager", "name": response["Name"], "arn": response["ARN"]}

    async def _delete_secretsmanager(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Secret name required"}
        sm = self._client("secretsmanager")
        sm.delete_secret(SecretId=resource_name, ForceDeleteWithoutRecovery=True)
        return {"success": True, "action": "delete", "resource_type": "secretsmanager", "name": resource_name, "message": f"Secret '{resource_name}' deleted"}

    async def _list_secretsmanager(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        sm = self._client("secretsmanager")
        response = sm.list_secrets()
        secrets = [{"name": s["Name"], "arn": s["ARN"]} for s in response["SecretList"]]
        return {"success": True, "action": "list", "resource_type": "secretsmanager", "secrets": secrets, "count": len(secrets)}

    # ===================== KMS =====================

    async def _create_kms(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        kms = self._client("kms")
        description = parameters.get("description", f"Key created by AI Platform: {resource_name or 'auto'}")
        tag_list = [{"TagKey": k, "TagValue": v} for k, v in (tags or {}).items()]
        response = kms.create_key(Description=description, Tags=tag_list)
        key = response["KeyMetadata"]
        if resource_name:
            kms.create_alias(AliasName=f"alias/{resource_name}", TargetKeyId=key["KeyId"])
        return {"success": True, "action": "create", "resource_type": "kms", "key_id": key["KeyId"], "arn": key["Arn"], "alias": resource_name}

    async def _delete_kms(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Key ID or alias required"}
        kms = self._client("kms")
        key_id = resource_name
        if not resource_name.startswith("arn:") and not resource_name.startswith("alias/"):
            key_id = f"alias/{resource_name}"
        kms.schedule_key_deletion(KeyId=key_id, PendingWindowInDays=7)
        return {"success": True, "action": "delete", "resource_type": "kms", "key": resource_name, "message": f"Key '{resource_name}' scheduled for deletion in 7 days"}

    async def _list_kms(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        kms = self._client("kms")
        response = kms.list_keys()
        keys = [{"key_id": k["KeyId"], "arn": k["KeyArn"]} for k in response["Keys"]]
        return {"success": True, "action": "list", "resource_type": "kms", "keys": keys, "count": len(keys)}

    # ===================== ECR =====================

    async def _create_ecr(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ecr = self._client("ecr")
        repo_name = resource_name or f"ai-repo-{uuid.uuid4().hex[:8]}"
        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]
        response = ecr.create_repository(
            repositoryName=repo_name,
            imageScanningConfiguration={"scanOnPush": True},
            encryptionConfiguration={"encryptionType": "AES256"},
            tags=tag_list,
        )
        repo = response["repository"]
        return {"success": True, "action": "create", "resource_type": "ecr", "repository_name": repo["repositoryName"], "uri": repo["repositoryUri"], "arn": repo["repositoryArn"]}

    async def _delete_ecr(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Repository name required"}
        ecr = self._client("ecr")
        ecr.delete_repository(repositoryName=resource_name, force=True)
        return {"success": True, "action": "delete", "resource_type": "ecr", "repository_name": resource_name, "message": f"ECR repository '{resource_name}' deleted"}

    async def _list_ecr(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ecr = self._client("ecr")
        response = ecr.describe_repositories()
        repos = [{"name": r["repositoryName"], "uri": r["repositoryUri"]} for r in response["repositories"]]
        return {"success": True, "action": "list", "resource_type": "ecr", "repositories": repos, "count": len(repos)}

    # ===================== EFS =====================

    async def _create_efs(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        efs = self._client("efs")
        all_tags = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]
        if resource_name:
            all_tags.append({"Key": "Name", "Value": resource_name})
        response = efs.create_file_system(
            CreationToken=resource_name or str(uuid.uuid4()),
            Encrypted=parameters.get("encryption", True),
            PerformanceMode=parameters.get("performance_mode", "generalPurpose"),
            Tags=all_tags,
        )
        return {"success": True, "action": "create", "resource_type": "efs", "file_system_id": response["FileSystemId"], "name": resource_name, "lifecycle_state": response["LifeCycleState"]}

    async def _delete_efs(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "File system ID required"}
        efs = self._client("efs")
        fs_id = resource_name
        if not resource_name.startswith("fs-"):
            resp = efs.describe_file_systems()
            for fs in resp["FileSystems"]:
                for t in fs.get("Tags", []):
                    if t["Key"] == "Name" and t["Value"] == resource_name:
                        fs_id = fs["FileSystemId"]
                        break
        efs.delete_file_system(FileSystemId=fs_id)
        return {"success": True, "action": "delete", "resource_type": "efs", "file_system_id": fs_id, "message": f"EFS '{fs_id}' deleted"}

    async def _list_efs(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        efs = self._client("efs")
        response = efs.describe_file_systems()
        systems = []
        for fs in response["FileSystems"]:
            name = ""
            for t in fs.get("Tags", []):
                if t["Key"] == "Name":
                    name = t["Value"]
            systems.append({"id": fs["FileSystemId"], "name": name, "state": fs["LifeCycleState"], "size_bytes": fs.get("SizeInBytes", {}).get("Value", 0)})
        return {"success": True, "action": "list", "resource_type": "efs", "file_systems": systems, "count": len(systems)}

    # ===================== ACM =====================

    async def _create_acm(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        acm = self._client("acm")
        domain = resource_name or parameters.get("domain")
        if not domain:
            return {"success": False, "error": "Domain name required for certificate"}
        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]
        response = acm.request_certificate(DomainName=domain, ValidationMethod="DNS", Tags=tag_list)
        return {"success": True, "action": "create", "resource_type": "acm", "certificate_arn": response["CertificateArn"], "domain": domain}

    async def _delete_acm(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Certificate ARN required"}
        acm = self._client("acm")
        acm.delete_certificate(CertificateArn=resource_name)
        return {"success": True, "action": "delete", "resource_type": "acm", "certificate_arn": resource_name}

    async def _list_acm(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        acm = self._client("acm")
        response = acm.list_certificates()
        certs = [{"arn": c["CertificateArn"], "domain": c["DomainName"], "status": c.get("Status")} for c in response["CertificateSummaryList"]]
        return {"success": True, "action": "list", "resource_type": "acm", "certificates": certs, "count": len(certs)}

    # ===================== SSM Parameter Store =====================

    async def _create_ssm(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ssm = self._client("ssm")
        param_name = resource_name or f"/ai-platform/{uuid.uuid4().hex[:8]}"
        if not param_name.startswith("/"):
            param_name = f"/{param_name}"
        value = parameters.get("value", "")
        param_type = parameters.get("type", "String")
        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]
        ssm.put_parameter(Name=param_name, Value=value or "placeholder", Type=param_type, Tags=tag_list, Overwrite=False)
        return {"success": True, "action": "create", "resource_type": "ssm", "parameter_name": param_name, "type": param_type}

    async def _delete_ssm(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Parameter name required"}
        ssm = self._client("ssm")
        if not resource_name.startswith("/"):
            resource_name = f"/{resource_name}"
        ssm.delete_parameter(Name=resource_name)
        return {"success": True, "action": "delete", "resource_type": "ssm", "parameter_name": resource_name}

    async def _list_ssm(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ssm = self._client("ssm")
        response = ssm.describe_parameters()
        params = [{"name": p["Name"], "type": p["Type"]} for p in response["Parameters"]]
        return {"success": True, "action": "list", "resource_type": "ssm", "parameters": params, "count": len(params)}

    # ===================== Route53 =====================

    async def _create_route53(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        r53 = self._client("route53")
        domain = resource_name or parameters.get("domain")
        if not domain:
            return {"success": False, "error": "Domain name required for hosted zone"}
        response = r53.create_hosted_zone(Name=domain, CallerReference=str(uuid.uuid4()))
        zone = response["HostedZone"]
        return {"success": True, "action": "create", "resource_type": "route53", "hosted_zone_id": zone["Id"], "domain": domain, "nameservers": response.get("DelegationSet", {}).get("NameServers", [])}

    async def _delete_route53(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Hosted zone ID required"}
        r53 = self._client("route53")
        r53.delete_hosted_zone(Id=resource_name)
        return {"success": True, "action": "delete", "resource_type": "route53", "hosted_zone_id": resource_name}

    async def _list_route53(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        r53 = self._client("route53")
        response = r53.list_hosted_zones()
        zones = [{"id": z["Id"], "name": z["Name"], "count": z["ResourceRecordSetCount"]} for z in response["HostedZones"]]
        return {"success": True, "action": "list", "resource_type": "route53", "hosted_zones": zones, "count": len(zones)}

    # ===================== API Gateway =====================

    async def _create_apigateway(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        apigw = self._client("apigateway")
        api_name = resource_name or f"ai-api-{uuid.uuid4().hex[:8]}"
        response = apigw.create_rest_api(name=api_name, description=parameters.get("description", "Created by AI Platform"), tags=tags or {})
        return {"success": True, "action": "create", "resource_type": "apigateway", "api_id": response["id"], "api_name": response["name"]}

    async def _delete_apigateway(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "API ID or name required"}
        apigw = self._client("apigateway")
        api_id = resource_name
        # Find by name if not an ID
        if len(resource_name) > 10:
            apis = apigw.get_rest_apis()
            for api in apis["items"]:
                if api["name"] == resource_name:
                    api_id = api["id"]
                    break
        apigw.delete_rest_api(restApiId=api_id)
        return {"success": True, "action": "delete", "resource_type": "apigateway", "api_id": api_id}

    async def _list_apigateway(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        apigw = self._client("apigateway")
        response = apigw.get_rest_apis()
        apis = [{"id": a["id"], "name": a["name"]} for a in response["items"]]
        return {"success": True, "action": "list", "resource_type": "apigateway", "apis": apis, "count": len(apis)}

    # ===================== CloudFront =====================

    async def _create_cloudfront(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        cf = self._client("cloudfront")
        origin_domain = parameters.get("origin_domain") or f"{resource_name}.s3.amazonaws.com" if resource_name else None
        if not origin_domain:
            return {"success": False, "error": "Origin domain required for CloudFront distribution"}

        config = {
            "CallerReference": str(uuid.uuid4()),
            "Comment": resource_name or "AI Platform Distribution",
            "Enabled": True,
            "Origins": {"Quantity": 1, "Items": [{"Id": "origin1", "DomainName": origin_domain, "S3OriginConfig": {"OriginAccessIdentity": ""}}]},
            "DefaultCacheBehavior": {
                "TargetOriginId": "origin1",
                "ViewerProtocolPolicy": "redirect-to-https",
                "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
                "ForwardedValues": {"QueryString": False, "Cookies": {"Forward": "none"}},
                "MinTTL": 0,
            },
        }
        response = cf.create_distribution(DistributionConfig=config)
        dist = response["Distribution"]
        return {"success": True, "action": "create", "resource_type": "cloudfront", "distribution_id": dist["Id"], "domain_name": dist["DomainName"], "status": dist["Status"]}

    async def _list_cloudfront(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        cf = self._client("cloudfront")
        response = cf.list_distributions()
        items = response.get("DistributionList", {}).get("Items", [])
        dists = [{"id": d["Id"], "domain": d["DomainName"], "status": d["Status"]} for d in items]
        return {"success": True, "action": "list", "resource_type": "cloudfront", "distributions": dists, "count": len(dists)}

    # ===================== Step Functions =====================

    async def _create_stepfunctions(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        sfn = self._client("stepfunctions")
        sm_name = resource_name or f"ai-workflow-{uuid.uuid4().hex[:8]}"
        role_arn = parameters.get("role_arn", "")
        definition = parameters.get("definition", json.dumps({"StartAt": "HelloWorld", "States": {"HelloWorld": {"Type": "Pass", "End": True}}}))

        if not role_arn:
            role_result = await self.ensure_service_role(
                service_slug="StepFunctionsExec",
                service_principal="states.amazonaws.com",
                policy_arns=["arn:aws:iam::aws:policy/AWSStepFunctionsFullAccess"],
                environment=str(parameters.get("environment") or "dev"),
                role_name=f"{sm_name}-sfn-role",
                tags=tags or {},
            )
            if not role_result.get("success"):
                return {"success": False, "error": f"Role ARN required for Step Functions state machine: {role_result.get('error')}"}
            role_arn = role_result.get("role_arn")

        tag_list = [{"key": k, "value": v} for k, v in (tags or {}).items()]
        response = sfn.create_state_machine(name=sm_name, definition=definition, roleArn=role_arn, tags=tag_list)
        return {"success": True, "action": "create", "resource_type": "stepfunctions", "state_machine_arn": response["stateMachineArn"], "name": sm_name}

    async def _list_stepfunctions(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        sfn = self._client("stepfunctions")
        response = sfn.list_state_machines()
        machines = [{"name": m["name"], "arn": m["stateMachineArn"]} for m in response["stateMachines"]]
        return {"success": True, "action": "list", "resource_type": "stepfunctions", "state_machines": machines, "count": len(machines)}

    # ===================== Elasticache =====================

    async def _create_elasticache(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec = self._client("elasticache")
        cluster_id = resource_name or f"ai-cache-{uuid.uuid4().hex[:8]}"
        engine = parameters.get("engine", "redis")
        node_type = parameters.get("instance_type", "cache.t3.micro")
        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]

        response = ec.create_cache_cluster(
            CacheClusterId=cluster_id, Engine=engine, CacheNodeType=node_type,
            NumCacheNodes=parameters.get("num_nodes", 1), Tags=tag_list,
        )
        cluster = response["CacheCluster"]
        return {"success": True, "action": "create", "resource_type": "elasticache", "cluster_id": cluster["CacheClusterId"], "engine": engine, "node_type": node_type, "status": cluster["CacheClusterStatus"]}

    async def _delete_elasticache(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Cache cluster ID required"}
        ec = self._client("elasticache")
        ec.delete_cache_cluster(CacheClusterId=resource_name)
        return {"success": True, "action": "delete", "resource_type": "elasticache", "cluster_id": resource_name, "message": f"Cache cluster '{resource_name}' deletion initiated"}

    async def _list_elasticache(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec = self._client("elasticache")
        response = ec.describe_cache_clusters()
        clusters = [{"id": c["CacheClusterId"], "engine": c["Engine"], "status": c["CacheClusterStatus"]} for c in response["CacheClusters"]]
        return {"success": True, "action": "list", "resource_type": "elasticache", "clusters": clusters, "count": len(clusters)}

    # ===================== Kinesis =====================

    async def _create_kinesis(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        kinesis = self._client("kinesis")
        stream_name = resource_name or f"ai-stream-{uuid.uuid4().hex[:8]}"
        shard_count = parameters.get("shard_count", 1)
        kinesis.create_stream(StreamName=stream_name, ShardCount=shard_count)
        return {"success": True, "action": "create", "resource_type": "kinesis", "stream_name": stream_name, "shard_count": shard_count}

    async def _delete_kinesis(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Stream name required"}
        kinesis = self._client("kinesis")
        kinesis.delete_stream(StreamName=resource_name, EnforceConsumerDeletion=True)
        return {"success": True, "action": "delete", "resource_type": "kinesis", "stream_name": resource_name}

    async def _list_kinesis(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        kinesis = self._client("kinesis")
        response = kinesis.list_streams()
        return {"success": True, "action": "list", "resource_type": "kinesis", "streams": response["StreamNames"], "count": len(response["StreamNames"])}

    # ===================== EKS =====================

    async def _create_eks(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        eks = self._client("eks")
        cluster_name = resource_name or f"ai-eks-{uuid.uuid4().hex[:8]}"
        role_arn = parameters.get("role_arn", "")
        version = parameters.get("version", parameters.get("kubernetes_version", "1.29"))

        if not role_arn:
            role_result = await self.ensure_eks_cluster_role(
                environment=str(parameters.get("environment") or "dev"),
                cluster_name=cluster_name,
                tags=tags or {},
            )
            if not role_result.get("success"):
                return {"success": False, "error": f"Failed to create EKS role: {role_result.get('error')}"}
            role_arn = role_result.get("role_arn")

        # Get subnets - EKS needs at least 2 subnets in different AZs
        subnet_ids = parameters.get("subnet_ids", [])
        security_group_ids = parameters.get("security_group_ids", [])

        if not subnet_ids:
            subnet_result = await self.discover_eks_subnet_ids(
                vpc_id=parameters.get("vpc_id"),
                min_count=2,
            )
            if subnet_result.get("success"):
                subnet_ids = subnet_result.get("subnet_ids", [])

        if len(subnet_ids) < 2:
            return {"success": False, "error": "EKS requires at least 2 subnets in different AZs. Provide subnet_ids or ensure default VPC has subnets."}

        create_args = {
            "name": cluster_name,
            "version": str(version),
            "roleArn": role_arn,
            "resourcesVpcConfig": {"subnetIds": subnet_ids},
            "tags": tags or {},
        }
        if security_group_ids:
            create_args["resourcesVpcConfig"]["securityGroupIds"] = security_group_ids

        print(f"  Creating EKS cluster: {cluster_name}, version={version}")
        response = eks.create_cluster(**create_args)
        cluster = response["cluster"]
        return {
            "success": True, "action": "create", "resource_type": "eks",
            "cluster_name": cluster["name"], "arn": cluster["arn"],
            "version": cluster.get("version"), "status": cluster["status"],
            "endpoint": cluster.get("endpoint"), "role_arn": role_arn,
        }

    async def _update_eks(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        params = parameters or {}
        cluster_name = (
            resource_name
            or params.get("cluster_name")
            or params.get("existing_resource_id")
            or params.get("target_resource_id")
        )
        if not cluster_name:
            return {"success": False, "error": "EKS cluster name is required for update workflow."}

        return await self.run_eks_workflow(
            cluster_name=str(cluster_name),
            parameters=params,
            tags=tags or {},
            environment=str(params.get("environment") or "dev"),
        )

    async def _delete_eks(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "EKS cluster name required for deletion"}
        eks = self._client("eks")

        # Delete nodegroups first
        try:
            nodegroups = eks.list_nodegroups(clusterName=resource_name)
            for ng in nodegroups.get("nodegroups", []):
                eks.delete_nodegroup(clusterName=resource_name, nodegroupName=ng)
                print(f"  Deleting nodegroup: {ng}")
        except Exception:
            pass

        eks.delete_cluster(name=resource_name)
        return {"success": True, "action": "delete", "resource_type": "eks", "cluster_name": resource_name, "message": f"EKS cluster '{resource_name}' deletion initiated"}

    async def _list_eks(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        eks = self._client("eks")
        response = eks.list_clusters()
        clusters = []
        for name in response.get("clusters", []):
            try:
                detail = eks.describe_cluster(name=name)["cluster"]
                clusters.append({"name": detail["name"], "arn": detail["arn"], "version": detail.get("version"), "status": detail["status"]})
            except Exception:
                clusters.append({"name": name})
        return {"success": True, "action": "list", "resource_type": "eks", "clusters": clusters, "count": len(clusters)}

    async def _describe_eks(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "EKS cluster name required"}
        eks = self._client("eks")
        response = eks.describe_cluster(name=resource_name)
        c = response["cluster"]
        return {
            "success": True, "action": "describe", "resource_type": "eks",
            "cluster_name": c["name"], "arn": c["arn"], "version": c.get("version"),
            "status": c["status"], "endpoint": c.get("endpoint"),
            "platform_version": c.get("platformVersion"),
            "vpc_config": c.get("resourcesVpcConfig", {}),
        }

    # ===================== ELB (Elastic Load Balancer) =====================

    async def _create_elb(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        elbv2 = self._client("elbv2")
        lb_name = resource_name or f"ai-lb-{uuid.uuid4().hex[:8]}"
        lb_type = parameters.get("type", "application")
        scheme = parameters.get("scheme", "internet-facing")

        # Get subnets
        subnet_ids = parameters.get("subnet_ids", [])
        if not subnet_ids:
            net = await self.discover_default_network_context(min_subnets=2)
            if net.get("success"):
                subnet_ids = self._to_list(net.get("subnet_ids"))[:2]

        if len(subnet_ids) < 2:
            return {"success": False, "error": "Load balancer requires at least 2 subnets."}

        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]
        print(f"  Creating {lb_type} load balancer: {lb_name}")

        response = elbv2.create_load_balancer(
            Name=lb_name, Subnets=subnet_ids,
            Scheme=scheme, Type=lb_type,
            Tags=tag_list,
        )
        lb = response["LoadBalancers"][0]
        return {
            "success": True, "action": "create", "resource_type": "elb",
            "load_balancer_name": lb["LoadBalancerName"], "arn": lb["LoadBalancerArn"],
            "dns_name": lb["DNSName"], "type": lb["Type"],
            "scheme": lb["Scheme"], "state": lb["State"]["Code"],
        }

    async def _delete_elb(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Load balancer name or ARN required"}
        elbv2 = self._client("elbv2")
        lb_arn = resource_name
        if not resource_name.startswith("arn:"):
            lbs = elbv2.describe_load_balancers(Names=[resource_name])
            if lbs["LoadBalancers"]:
                lb_arn = lbs["LoadBalancers"][0]["LoadBalancerArn"]
        elbv2.delete_load_balancer(LoadBalancerArn=lb_arn)
        return {"success": True, "action": "delete", "resource_type": "elb", "name": resource_name, "message": f"Load balancer '{resource_name}' deletion initiated"}

    async def _list_elb(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        elbv2 = self._client("elbv2")
        response = elbv2.describe_load_balancers()
        lbs = [{"name": lb["LoadBalancerName"], "arn": lb["LoadBalancerArn"], "dns": lb["DNSName"], "type": lb["Type"], "state": lb["State"]["Code"]} for lb in response["LoadBalancers"]]
        return {"success": True, "action": "list", "resource_type": "elb", "load_balancers": lbs, "count": len(lbs)}

    async def _describe_elb(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Load balancer name required"}
        elbv2 = self._client("elbv2")
        if resource_name.startswith("arn:"):
            response = elbv2.describe_load_balancers(LoadBalancerArns=[resource_name])
        else:
            response = elbv2.describe_load_balancers(Names=[resource_name])
        if not response["LoadBalancers"]:
            return {"success": False, "error": f"Load balancer '{resource_name}' not found"}
        lb = response["LoadBalancers"][0]
        return {
            "success": True, "action": "describe", "resource_type": "elb",
            "name": lb["LoadBalancerName"], "arn": lb["LoadBalancerArn"],
            "dns_name": lb["DNSName"], "type": lb["Type"],
            "scheme": lb["Scheme"], "state": lb["State"]["Code"],
            "vpc_id": lb.get("VpcId"),
        }

    # ===================== WAF =====================

    async def _create_waf(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        wafv2 = self._client("wafv2")
        acl_name = resource_name or f"ai-waf-{uuid.uuid4().hex[:8]}"
        scope = parameters.get("scope", "REGIONAL")
        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]

        response = wafv2.create_web_acl(
            Name=acl_name, Scope=scope,
            DefaultAction={"Allow": {}},
            VisibilityConfig={"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": acl_name},
            Rules=[], Tags=tag_list,
        )
        acl = response["Summary"]
        return {"success": True, "action": "create", "resource_type": "waf", "name": acl["Name"], "arn": acl["ARN"], "id": acl["Id"]}

    async def _delete_waf(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "WAF Web ACL name required"}
        wafv2 = self._client("wafv2")
        scope = parameters.get("scope", "REGIONAL")
        acls = wafv2.list_web_acls(Scope=scope)
        for acl in acls.get("WebACLs", []):
            if acl["Name"] == resource_name:
                wafv2.delete_web_acl(Name=resource_name, Scope=scope, Id=acl["Id"], LockToken=acl["LockToken"])
                return {"success": True, "action": "delete", "resource_type": "waf", "name": resource_name, "message": f"WAF '{resource_name}' deleted"}
        return {"success": False, "error": f"WAF Web ACL '{resource_name}' not found"}

    async def _list_waf(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        wafv2 = self._client("wafv2")
        scope = parameters.get("scope", "REGIONAL")
        response = wafv2.list_web_acls(Scope=scope)
        acls = [{"name": a["Name"], "arn": a["ARN"], "id": a["Id"]} for a in response.get("WebACLs", [])]
        return {"success": True, "action": "list", "resource_type": "waf", "web_acls": acls, "count": len(acls)}

    # ===================== Redshift =====================

    async def _create_redshift(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        rs = self._client("redshift")
        cluster_id = resource_name or f"ai-redshift-{uuid.uuid4().hex[:8]}"
        node_type = parameters.get("node_type", parameters.get("instance_type", "dc2.large"))
        num_nodes = parameters.get("num_nodes", 1)
        db_name = parameters.get("db_name", "aidb")
        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]

        print(f"  Creating Redshift cluster: {cluster_id}")
        create_args = {
            "ClusterIdentifier": cluster_id,
            "NodeType": node_type,
            "MasterUsername": parameters.get("master_username", "admin"),
            "MasterUserPassword": parameters.get("master_password", f"AutoPass-{uuid.uuid4().hex[:12]}"),
            "DBName": db_name,
            "Tags": tag_list,
            "Encrypted": parameters.get("encryption", True),
        }
        if num_nodes > 1:
            create_args["ClusterType"] = "multi-node"
            create_args["NumberOfNodes"] = num_nodes
        else:
            create_args["ClusterType"] = "single-node"

        response = rs.create_cluster(**create_args)
        cluster = response["Cluster"]
        return {
            "success": True, "action": "create", "resource_type": "redshift",
            "cluster_id": cluster["ClusterIdentifier"], "node_type": node_type,
            "num_nodes": num_nodes, "db_name": db_name,
            "status": cluster["ClusterStatus"],
        }

    async def _delete_redshift(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Redshift cluster identifier required"}
        rs = self._client("redshift")
        rs.delete_cluster(ClusterIdentifier=resource_name, SkipFinalClusterSnapshot=True)
        return {"success": True, "action": "delete", "resource_type": "redshift", "cluster_id": resource_name, "message": f"Redshift cluster '{resource_name}' deletion initiated"}

    async def _list_redshift(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        rs = self._client("redshift")
        response = rs.describe_clusters()
        clusters = [{"id": c["ClusterIdentifier"], "node_type": c["NodeType"], "status": c["ClusterStatus"], "db": c.get("DBName")} for c in response["Clusters"]]
        return {"success": True, "action": "list", "resource_type": "redshift", "clusters": clusters, "count": len(clusters)}

    async def _describe_redshift(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Redshift cluster identifier required"}
        rs = self._client("redshift")
        response = rs.describe_clusters(ClusterIdentifier=resource_name)
        c = response["Clusters"][0]
        return {
            "success": True, "action": "describe", "resource_type": "redshift",
            "cluster_id": c["ClusterIdentifier"], "node_type": c["NodeType"],
            "status": c["ClusterStatus"], "db_name": c.get("DBName"),
            "endpoint": c.get("Endpoint", {}).get("Address"),
            "port": c.get("Endpoint", {}).get("Port"),
            "num_nodes": c.get("NumberOfNodes"),
        }

    # ===================== EMR =====================

    async def _create_emr(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        emr = self._client("emr")
        cluster_name = resource_name or f"ai-emr-{uuid.uuid4().hex[:8]}"
        release = parameters.get("release_label", "emr-7.0.0")
        master_type = parameters.get("master_instance_type", parameters.get("instance_type", "m5.xlarge"))
        worker_type = parameters.get("worker_instance_type", master_type)
        worker_count = parameters.get("instance_count", parameters.get("num_nodes", 2))
        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]

        if self._to_bool(parameters.get("auto_create_default_roles", True), default=True):
            try:
                emr.create_default_roles()
                await asyncio.sleep(2)
            except Exception as e:
                text = str(e)
                if "already exists" not in text.lower() and "invalidrequestexception" not in text.lower():
                    print(f"  EMR default role ensure warning: {e}")

        print(f"  Creating EMR cluster: {cluster_name}")
        response = emr.run_job_flow(
            Name=cluster_name,
            ReleaseLabel=release,
            Instances={
                "MasterInstanceType": master_type,
                "SlaveInstanceType": worker_type,
                "InstanceCount": worker_count,
                "KeepJobFlowAliveWhenNoSteps": True,
                "TerminationProtected": False,
            },
            Applications=[{"Name": "Spark"}, {"Name": "Hive"}],
            VisibleToAllUsers=True,
            JobFlowRole="EMR_EC2_DefaultRole",
            ServiceRole="EMR_DefaultRole",
            Tags=tag_list,
        )
        return {
            "success": True, "action": "create", "resource_type": "emr",
            "cluster_id": response["JobFlowId"], "cluster_name": cluster_name,
            "release": release, "master_type": master_type, "worker_count": worker_count,
        }

    async def _delete_emr(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "EMR cluster ID required"}
        emr = self._client("emr")
        cluster_id = resource_name
        if not resource_name.startswith("j-"):
            clusters = emr.list_clusters(ClusterStates=["WAITING", "RUNNING", "STARTING"])
            for c in clusters.get("Clusters", []):
                if c["Name"] == resource_name:
                    cluster_id = c["Id"]
                    break
        emr.terminate_job_flows(JobFlowIds=[cluster_id])
        return {"success": True, "action": "delete", "resource_type": "emr", "cluster_id": cluster_id, "message": f"EMR cluster '{resource_name}' termination initiated"}

    async def _list_emr(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        emr = self._client("emr")
        response = emr.list_clusters(ClusterStates=["WAITING", "RUNNING", "STARTING", "BOOTSTRAPPING"])
        clusters = [{"id": c["Id"], "name": c["Name"], "status": c["Status"]["State"]} for c in response.get("Clusters", [])]
        return {"success": True, "action": "list", "resource_type": "emr", "clusters": clusters, "count": len(clusters)}

    # ===================== SageMaker =====================

    async def _create_sagemaker(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        sm = self._client("sagemaker")
        notebook_name = resource_name or f"ai-notebook-{uuid.uuid4().hex[:8]}"
        instance_type = parameters.get("instance_type", "ml.t3.medium")
        role_arn = parameters.get("role_arn", "")
        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]

        if not role_arn:
            role_result = await self.ensure_service_role(
                service_slug="SageMakerExec",
                service_principal="sagemaker.amazonaws.com",
                policy_arns=["arn:aws:iam::aws:policy/AmazonSageMakerFullAccess"],
                environment=str(parameters.get("environment") or "dev"),
                role_name=f"{notebook_name}-sagemaker-role",
                tags=tags or {},
            )
            if not role_result.get("success"):
                return {"success": False, "error": f"Failed to create SageMaker role: {role_result.get('error')}"}
            role_arn = role_result.get("role_arn")

        print(f"  Creating SageMaker notebook: {notebook_name}")
        response = sm.create_notebook_instance(
            NotebookInstanceName=notebook_name,
            InstanceType=instance_type,
            RoleArn=role_arn,
            Tags=tag_list,
        )
        return {
            "success": True, "action": "create", "resource_type": "sagemaker",
            "notebook_name": notebook_name, "arn": response["NotebookInstanceArn"],
            "instance_type": instance_type,
        }

    async def _delete_sagemaker(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "SageMaker notebook name required"}
        sm = self._client("sagemaker")
        try:
            sm.stop_notebook_instance(NotebookInstanceName=resource_name)
        except Exception:
            pass
        sm.delete_notebook_instance(NotebookInstanceName=resource_name)
        return {"success": True, "action": "delete", "resource_type": "sagemaker", "notebook_name": resource_name, "message": f"SageMaker notebook '{resource_name}' deletion initiated"}

    async def _list_sagemaker(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        sm = self._client("sagemaker")
        response = sm.list_notebook_instances()
        notebooks = [{"name": n["NotebookInstanceName"], "status": n["NotebookInstanceStatus"], "type": n["InstanceType"]} for n in response.get("NotebookInstances", [])]
        return {"success": True, "action": "list", "resource_type": "sagemaker", "notebooks": notebooks, "count": len(notebooks)}

    async def _describe_sagemaker(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "SageMaker notebook name required"}
        sm = self._client("sagemaker")
        n = sm.describe_notebook_instance(NotebookInstanceName=resource_name)
        return {
            "success": True, "action": "describe", "resource_type": "sagemaker",
            "name": n["NotebookInstanceName"], "arn": n["NotebookInstanceArn"],
            "status": n["NotebookInstanceStatus"], "instance_type": n["InstanceType"],
            "url": n.get("Url"),
        }

    # ===================== Glue =====================

    async def _create_glue(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        glue = self._client("glue")
        db_name = resource_name or f"ai-glue-{uuid.uuid4().hex[:8]}"

        glue_type = parameters.get("glue_type", "database")
        if glue_type == "crawler":
            role_arn = parameters.get("role_arn", "")
            if not role_arn:
                role_result = await self.ensure_service_role(
                    service_slug="GlueCrawlerExec",
                    service_principal="glue.amazonaws.com",
                    policy_arns=["arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"],
                    environment=str(parameters.get("environment") or "dev"),
                    role_name=f"{db_name}-glue-role",
                    tags=tags or {},
                )
                if not role_result.get("success"):
                    return {"success": False, "error": f"Role ARN required for Glue crawler: {role_result.get('error')}"}
                role_arn = role_result.get("role_arn")
            s3_target = parameters.get("s3_target", "")
            if not s3_target:
                return {"success": False, "error": "S3 target path required for Glue crawler"}
            glue.create_crawler(
                Name=db_name, Role=role_arn, DatabaseName=parameters.get("database", "default"),
                Targets={"S3Targets": [{"Path": s3_target}]},
                Tags=tags or {},
            )
            return {"success": True, "action": "create", "resource_type": "glue", "glue_type": "crawler", "crawler_name": db_name}
        else:
            glue.create_database(DatabaseInput={"Name": db_name, "Description": parameters.get("description", f"Created by AI Platform")})
            return {"success": True, "action": "create", "resource_type": "glue", "glue_type": "database", "database_name": db_name}

    async def _delete_glue(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Glue resource name required"}
        glue = self._client("glue")
        glue_type = parameters.get("glue_type", "database")
        if glue_type == "crawler":
            glue.delete_crawler(Name=resource_name)
        else:
            glue.delete_database(Name=resource_name)
        return {"success": True, "action": "delete", "resource_type": "glue", "name": resource_name, "message": f"Glue {glue_type} '{resource_name}' deleted"}

    async def _list_glue(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        glue = self._client("glue")
        dbs = glue.get_databases()
        databases = [{"name": d["Name"], "description": d.get("Description", "")} for d in dbs.get("DatabaseList", [])]
        crawlers = []
        try:
            cr = glue.get_crawlers()
            crawlers = [{"name": c["Name"], "state": c.get("State"), "database": c.get("DatabaseName")} for c in cr.get("Crawlers", [])]
        except Exception:
            pass
        return {"success": True, "action": "list", "resource_type": "glue", "databases": databases, "crawlers": crawlers, "count": len(databases) + len(crawlers)}

    # ===================== Athena =====================

    async def _create_athena(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        athena = self._client("athena")
        wg_name = resource_name or f"ai-athena-{uuid.uuid4().hex[:8]}"
        output_location = parameters.get("output_location", f"s3://aws-athena-query-results-{self.region}/")
        tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]

        athena.create_work_group(
            Name=wg_name,
            Configuration={"ResultConfiguration": {"OutputLocation": output_location}, "EnforceWorkGroupConfiguration": True},
            Tags=tag_list,
        )
        return {"success": True, "action": "create", "resource_type": "athena", "workgroup_name": wg_name, "output_location": output_location}

    async def _delete_athena(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Athena workgroup name required"}
        athena = self._client("athena")
        athena.delete_work_group(WorkGroup=resource_name, RecursiveDeleteOption=True)
        return {"success": True, "action": "delete", "resource_type": "athena", "workgroup_name": resource_name, "message": f"Athena workgroup '{resource_name}' deleted"}

    async def _list_athena(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        athena = self._client("athena")
        response = athena.list_work_groups()
        wgs = [{"name": w["Name"], "state": w.get("State"), "engine": w.get("EngineVersion", {}).get("SelectedEngineVersion")} for w in response.get("WorkGroups", [])]
        return {"success": True, "action": "list", "resource_type": "athena", "workgroups": wgs, "count": len(wgs)}

    # ===================== CodePipeline =====================

    async def _create_codepipeline(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        cp = self._client("codepipeline")
        pipeline_name = resource_name or f"ai-pipeline-{uuid.uuid4().hex[:8]}"
        role_arn = parameters.get("role_arn", "")
        if not role_arn:
            role_result = await self.ensure_service_role(
                service_slug="CodePipelineExec",
                service_principal="codepipeline.amazonaws.com",
                policy_arns=["arn:aws:iam::aws:policy/AWSCodePipeline_FullAccess"],
                environment=str(parameters.get("environment") or "dev"),
                role_name=f"{pipeline_name}-codepipeline-role",
                tags=tags or {},
            )
            if not role_result.get("success"):
                return {"success": False, "error": f"Role ARN required for CodePipeline: {role_result.get('error')}"}
            role_arn = role_result.get("role_arn")

        artifact_store = parameters.get("artifact_bucket", f"{pipeline_name}-artifacts")
        s3 = self._client("s3")
        try:
            if self.region == "us-east-1":
                s3.create_bucket(Bucket=artifact_store)
            else:
                s3.create_bucket(
                    Bucket=artifact_store,
                    CreateBucketConfiguration={"LocationConstraint": self.region},
                )
        except Exception as e:
            err = str(e)
            if "BucketAlreadyOwnedByYou" not in err:
                if "BucketAlreadyExists" in err:
                    artifact_store = f"{artifact_store}-{uuid.uuid4().hex[:6]}"
                    if self.region == "us-east-1":
                        s3.create_bucket(Bucket=artifact_store)
                    else:
                        s3.create_bucket(
                            Bucket=artifact_store,
                            CreateBucketConfiguration={"LocationConstraint": self.region},
                        )
                else:
                    return {"success": False, "error": f"Failed to prepare artifact bucket: {e}"}
        tag_list = [{"key": k, "value": v} for k, v in (tags or {}).items()]

        pipeline_def = {
            "name": pipeline_name,
            "roleArn": role_arn,
            "artifactStore": {"type": "S3", "location": artifact_store},
            "stages": [
                {
                    "name": "Source",
                    "actions": [{
                        "name": "SourceAction",
                        "actionTypeId": {"category": "Source", "owner": "AWS", "provider": "S3", "version": "1"},
                        "configuration": {"S3Bucket": artifact_store, "S3ObjectKey": "source.zip"},
                        "outputArtifacts": [{"name": "SourceOutput"}],
                    }],
                },
                {
                    "name": "Deploy",
                    "actions": [{
                        "name": "DeployAction",
                        "actionTypeId": {"category": "Deploy", "owner": "AWS", "provider": "S3", "version": "1"},
                        "configuration": {"BucketName": artifact_store, "Extract": "true"},
                        "inputArtifacts": [{"name": "SourceOutput"}],
                    }],
                },
            ],
        }

        response = cp.create_pipeline(pipeline=pipeline_def, tags=tag_list)
        p = response["pipeline"]
        return {"success": True, "action": "create", "resource_type": "codepipeline", "pipeline_name": p["name"], "version": p.get("version")}

    async def _delete_codepipeline(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Pipeline name required"}
        cp = self._client("codepipeline")
        cp.delete_pipeline(name=resource_name)
        return {"success": True, "action": "delete", "resource_type": "codepipeline", "pipeline_name": resource_name, "message": f"Pipeline '{resource_name}' deleted"}

    async def _list_codepipeline(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        cp = self._client("codepipeline")
        response = cp.list_pipelines()
        pipelines = [{"name": p["name"], "version": p.get("version"), "created": p.get("created", "").isoformat() if hasattr(p.get("created", ""), "isoformat") else str(p.get("created", ""))} for p in response.get("pipelines", [])]
        return {"success": True, "action": "list", "resource_type": "codepipeline", "pipelines": pipelines, "count": len(pipelines)}

    # ===================== CodeBuild =====================

    async def _create_codebuild(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        cb = self._client("codebuild")
        project_name = resource_name or f"ai-build-{uuid.uuid4().hex[:8]}"
        role_arn = parameters.get("role_arn", "")
        if not role_arn:
            role_result = await self.ensure_service_role(
                service_slug="CodeBuildExec",
                service_principal="codebuild.amazonaws.com",
                policy_arns=["arn:aws:iam::aws:policy/AWSCodeBuildDeveloperAccess"],
                environment=str(parameters.get("environment") or "dev"),
                role_name=f"{project_name}-codebuild-role",
                tags=tags or {},
            )
            if not role_result.get("success"):
                return {"success": False, "error": f"Service role ARN required for CodeBuild: {role_result.get('error')}"}
            role_arn = role_result.get("role_arn")

        compute_type = parameters.get("compute_type", "BUILD_GENERAL1_SMALL")
        image = parameters.get("image", "aws/codebuild/amazonlinux2-x86_64-standard:4.0")
        tag_list = [{"key": k, "value": v} for k, v in (tags or {}).items()]

        response = cb.create_project(
            name=project_name,
            source={"type": "NO_SOURCE", "buildspec": parameters.get("buildspec", "version: 0.2\nphases:\n  build:\n    commands:\n      - echo Build started")},
            artifacts={"type": "NO_ARTIFACTS"},
            environment={"type": "LINUX_CONTAINER", "image": image, "computeType": compute_type},
            serviceRole=role_arn,
            tags=tag_list,
        )
        p = response["project"]
        return {"success": True, "action": "create", "resource_type": "codebuild", "project_name": p["name"], "arn": p["arn"]}

    async def _delete_codebuild(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "CodeBuild project name required"}
        cb = self._client("codebuild")
        cb.delete_project(name=resource_name)
        return {"success": True, "action": "delete", "resource_type": "codebuild", "project_name": resource_name, "message": f"CodeBuild project '{resource_name}' deleted"}

    async def _list_codebuild(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        cb = self._client("codebuild")
        response = cb.list_projects()
        projects = response.get("projects", [])
        details = []
        if projects:
            batch = cb.batch_get_projects(names=projects[:20])
            details = [{"name": p["name"], "arn": p["arn"]} for p in batch.get("projects", [])]
        return {"success": True, "action": "list", "resource_type": "codebuild", "projects": details, "count": len(details)}

    # ===================== Well-Architected Tool =====================

    async def _create_wellarchitected(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        wa = self._client("wellarchitected")
        parameters = parameters or {}
        tags = tags or {}

        workload_name = (
            parameters.get("workload_name")
            or parameters.get("WorkloadName")
            or resource_name
            or f"ai-workload-{uuid.uuid4().hex[:8]}"
        )
        description = parameters.get("description") or parameters.get("Description") or "Workload created by AI Infra Platform"
        environment = self._normalize_wa_environment(parameters.get("environment") or parameters.get("Environment"))
        lenses = self._to_list(parameters.get("lenses") or parameters.get("Lenses")) or ["wellarchitected"]
        client_token = parameters.get("client_request_token") or str(uuid.uuid4())

        create_args = {
            "WorkloadName": workload_name,
            "Description": description,
            "Environment": environment,
            "Lenses": lenses,
            "ClientRequestToken": client_token,
        }

        optional_map = {
            "account_ids": "AccountIds",
            "aws_regions": "AwsRegions",
            "non_aws_regions": "NonAwsRegions",
            "pillar_priorities": "PillarPriorities",
            "applications": "Applications",
            "profile_arns": "ProfileArns",
            "review_template_arns": "ReviewTemplateArns",
            "architectural_design": "ArchitecturalDesign",
            "review_owner": "ReviewOwner",
            "industry_type": "IndustryType",
            "industry": "Industry",
            "notes": "Notes",
            "discovery_config": "DiscoveryConfig",
            "jira_configuration": "JiraConfiguration",
        }
        for key, api_key in optional_map.items():
            value = parameters.get(key)
            if value in (None, "", []):
                continue
            if isinstance(value, str) and api_key in {
                "AccountIds", "AwsRegions", "NonAwsRegions", "PillarPriorities", "Applications", "ProfileArns", "ReviewTemplateArns"
            }:
                value = self._to_list(value)
            create_args[api_key] = value

        merged_tags = {}
        request_tags = parameters.get("tags")
        if isinstance(request_tags, dict):
            merged_tags.update({str(k): str(v) for k, v in request_tags.items()})
        if isinstance(tags, dict):
            merged_tags.update({str(k): str(v) for k, v in tags.items()})
        if merged_tags:
            create_args["Tags"] = merged_tags

        response = wa.create_workload(**create_args)
        workload = response.get("Workload", {})
        return {
            "success": True,
            "action": "create",
            "resource_type": "wellarchitected",
            "workload_id": workload.get("WorkloadId"),
            "workload_name": workload.get("WorkloadName", workload_name),
            "workload_arn": workload.get("WorkloadArn"),
            "environment": workload.get("Environment", environment),
            "lenses": workload.get("Lenses", lenses),
            "owner": workload.get("Owner"),
            "region": self.region,
        }

    async def _list_wellarchitected(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        wa = self._client("wellarchitected")
        summaries = self._list_wellarchitected_workloads(wa)
        workloads = [
            {
                "workload_id": item.get("WorkloadId"),
                "workload_name": item.get("WorkloadName"),
                "owner": item.get("Owner"),
                "updated_at": item.get("UpdatedAt"),
                "improvement_status": item.get("ImprovementStatus"),
                "lenses": item.get("Lenses"),
            }
            for item in summaries
        ]
        return {
            "success": True,
            "action": "list",
            "resource_type": "wellarchitected",
            "workloads": workloads,
            "count": len(workloads),
            "region": self.region,
        }

    async def _describe_wellarchitected(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        wa = self._client("wellarchitected")
        parameters = parameters or {}
        workload_ref = (
            parameters.get("workload_id")
            or parameters.get("WorkloadId")
            or parameters.get("workload_name")
            or resource_name
        )
        workload_id = self._resolve_wellarchitected_workload_id(wa, workload_ref)
        if not workload_id:
            return {"success": False, "error": "Workload ID or existing workload name is required."}

        response = wa.get_workload(WorkloadId=workload_id)
        workload = response.get("Workload", {})
        return {
            "success": True,
            "action": "describe",
            "resource_type": "wellarchitected",
            "workload_id": workload.get("WorkloadId", workload_id),
            "workload_name": workload.get("WorkloadName"),
            "workload_arn": workload.get("WorkloadArn"),
            "description": workload.get("Description"),
            "environment": workload.get("Environment"),
            "lenses": workload.get("Lenses"),
            "owner": workload.get("Owner"),
            "review_owner": workload.get("ReviewOwner"),
            "improvement_status": workload.get("ImprovementStatus"),
            "risk_counts": workload.get("RiskCounts"),
            "updated_at": workload.get("UpdatedAt"),
            "region": self.region,
        }

    async def _update_wellarchitected(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        wa = self._client("wellarchitected")
        parameters = parameters or {}

        workload_ref = (
            parameters.get("workload_id")
            or parameters.get("WorkloadId")
            or parameters.get("workload_name")
            or resource_name
        )
        workload_id = self._resolve_wellarchitected_workload_id(wa, workload_ref)
        if not workload_id:
            return {"success": False, "error": "Workload ID or existing workload name is required for update."}

        update_args = {"WorkloadId": workload_id}
        update_map = {
            "workload_name": "WorkloadName",
            "description": "Description",
            "environment": "Environment",
            "account_ids": "AccountIds",
            "aws_regions": "AwsRegions",
            "non_aws_regions": "NonAwsRegions",
            "pillar_priorities": "PillarPriorities",
            "architectural_design": "ArchitecturalDesign",
            "review_owner": "ReviewOwner",
            "is_review_owner_update_acknowledged": "IsReviewOwnerUpdateAcknowledged",
            "industry_type": "IndustryType",
            "industry": "Industry",
            "notes": "Notes",
            "improvement_status": "ImprovementStatus",
            "discovery_config": "DiscoveryConfig",
            "applications": "Applications",
            "jira_configuration": "JiraConfiguration",
        }
        list_keys = {
            "account_ids",
            "aws_regions",
            "non_aws_regions",
            "pillar_priorities",
            "applications",
        }

        for key, api_key in update_map.items():
            value = parameters.get(key)
            if value in (None, "", []):
                continue
            if key == "environment":
                value = self._normalize_wa_environment(value)
            if key == "is_review_owner_update_acknowledged":
                value = self._to_bool(value)
            if key in list_keys and isinstance(value, str):
                value = self._to_list(value)
            update_args[api_key] = value

        if len(update_args) == 1:
            return {"success": False, "error": "No update fields provided for workload update."}

        response = wa.update_workload(**update_args)
        workload = response.get("Workload", {})
        return {
            "success": True,
            "action": "update",
            "resource_type": "wellarchitected",
            "workload_id": workload.get("WorkloadId", workload_id),
            "workload_name": workload.get("WorkloadName"),
            "environment": workload.get("Environment"),
            "improvement_status": workload.get("ImprovementStatus"),
            "updated_at": workload.get("UpdatedAt"),
            "region": self.region,
            "changes_applied": sorted([k for k in update_args.keys() if k != "WorkloadId"]),
        }

    async def _delete_wellarchitected(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        wa = self._client("wellarchitected")
        parameters = parameters or {}

        workload_ref = (
            parameters.get("workload_id")
            or parameters.get("WorkloadId")
            or parameters.get("workload_name")
            or resource_name
        )
        workload_id = self._resolve_wellarchitected_workload_id(wa, workload_ref)
        if not workload_id:
            return {"success": False, "error": "Workload ID or existing workload name is required for deletion."}

        wa.delete_workload(
            WorkloadId=workload_id,
            ClientRequestToken=str(uuid.uuid4()),
        )
        return {
            "success": True,
            "action": "delete",
            "resource_type": "wellarchitected",
            "workload_id": workload_id,
            "message": f"Well-Architected workload '{workload_id}' deleted successfully",
            "region": self.region,
        }

    # ===================== Subnet =====================

    async def _create_subnet(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        vpc_id = parameters.get("vpc_id")
        cidr = parameters.get("cidr_block", "10.0.1.0/24")
        az = parameters.get("availability_zone", f"{self.region}a")

        if not vpc_id:
            vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
            if vpcs["Vpcs"]:
                vpc_id = vpcs["Vpcs"][0]["VpcId"]
            else:
                return {"success": False, "error": "VPC ID required for subnet creation"}

        all_tags = {**(tags or {})}
        if resource_name:
            all_tags["Name"] = resource_name

        response = ec2.create_subnet(
            VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=az,
            TagSpecifications=[{"ResourceType": "subnet", "Tags": [{"Key": k, "Value": v} for k, v in all_tags.items()]}],
        )
        subnet = response["Subnet"]
        return {
            "success": True, "action": "create", "resource_type": "subnet",
            "subnet_id": subnet["SubnetId"], "vpc_id": vpc_id,
            "cidr_block": cidr, "availability_zone": az, "name": resource_name,
        }

    async def _delete_subnet(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        subnet_id = parameters.get("subnet_id") or resource_name
        if not subnet_id:
            return {"success": False, "error": "Subnet ID required"}
        if not subnet_id.startswith("subnet-"):
            resp = ec2.describe_subnets(Filters=[{"Name": "tag:Name", "Values": [subnet_id]}])
            if resp["Subnets"]:
                subnet_id = resp["Subnets"][0]["SubnetId"]
        ec2.delete_subnet(SubnetId=subnet_id)
        return {"success": True, "action": "delete", "resource_type": "subnet", "subnet_id": subnet_id, "message": f"Subnet {subnet_id} deleted"}

    async def _list_subnet(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        response = ec2.describe_subnets()
        subnets = []
        for s in response["Subnets"]:
            name = ""
            for t in s.get("Tags", []):
                if t["Key"] == "Name":
                    name = t["Value"]
            subnets.append({"subnet_id": s["SubnetId"], "name": name, "vpc_id": s["VpcId"], "cidr": s["CidrBlock"], "az": s["AvailabilityZone"], "state": s["State"]})
        return {"success": True, "action": "list", "resource_type": "subnet", "subnets": subnets, "count": len(subnets)}

    # ===================== NAT Gateway =====================

    async def _create_nat_gateway(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        subnet_id = parameters.get("subnet_id")
        if not subnet_id:
            return {"success": False, "error": "Subnet ID required for NAT Gateway"}

        alloc = ec2.allocate_address(Domain="vpc")
        allocation_id = alloc["AllocationId"]
        all_tags = {**(tags or {})}
        if resource_name:
            all_tags["Name"] = resource_name

        response = ec2.create_nat_gateway(
            SubnetId=subnet_id, AllocationId=allocation_id,
            TagSpecifications=[{"ResourceType": "natgateway", "Tags": [{"Key": k, "Value": v} for k, v in all_tags.items()]}],
        )
        ngw = response["NatGateway"]
        return {
            "success": True, "action": "create", "resource_type": "nat_gateway",
            "nat_gateway_id": ngw["NatGatewayId"], "subnet_id": subnet_id,
            "elastic_ip": alloc.get("PublicIp"), "state": ngw["State"],
        }

    async def _delete_nat_gateway(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        ngw_id = parameters.get("nat_gateway_id") or resource_name
        if not ngw_id:
            return {"success": False, "error": "NAT Gateway ID required"}
        if not ngw_id.startswith("nat-"):
            resp = ec2.describe_nat_gateways(Filters=[{"Name": "tag:Name", "Values": [ngw_id]}])
            if resp["NatGateways"]:
                ngw_id = resp["NatGateways"][0]["NatGatewayId"]
        ec2.delete_nat_gateway(NatGatewayId=ngw_id)
        return {"success": True, "action": "delete", "resource_type": "nat_gateway", "nat_gateway_id": ngw_id, "message": f"NAT Gateway {ngw_id} deletion initiated"}

    async def _list_nat_gateway(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        response = ec2.describe_nat_gateways()
        ngws = []
        for n in response.get("NatGateways", []):
            name = ""
            for t in n.get("Tags", []):
                if t["Key"] == "Name":
                    name = t["Value"]
            ngws.append({"id": n["NatGatewayId"], "name": name, "state": n["State"], "subnet_id": n.get("SubnetId"), "vpc_id": n.get("VpcId")})
        return {"success": True, "action": "list", "resource_type": "nat_gateway", "nat_gateways": ngws, "count": len(ngws)}

    # ===================== Internet Gateway =====================

    async def _create_internet_gateway(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        all_tags = {**(tags or {})}
        if resource_name:
            all_tags["Name"] = resource_name

        response = ec2.create_internet_gateway(
            TagSpecifications=[{"ResourceType": "internet-gateway", "Tags": [{"Key": k, "Value": v} for k, v in all_tags.items()]}],
        )
        igw = response["InternetGateway"]
        vpc_id = parameters.get("vpc_id")
        if vpc_id:
            ec2.attach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
        return {
            "success": True, "action": "create", "resource_type": "internet_gateway",
            "internet_gateway_id": igw["InternetGatewayId"], "name": resource_name,
            "attached_vpc": vpc_id,
        }

    async def _delete_internet_gateway(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        igw_id = parameters.get("internet_gateway_id") or resource_name
        if not igw_id:
            return {"success": False, "error": "Internet Gateway ID required"}
        if not igw_id.startswith("igw-"):
            resp = ec2.describe_internet_gateways(Filters=[{"Name": "tag:Name", "Values": [igw_id]}])
            if resp["InternetGateways"]:
                igw_id = resp["InternetGateways"][0]["InternetGatewayId"]
        # Detach from VPC first
        try:
            igw_info = ec2.describe_internet_gateways(InternetGatewayIds=[igw_id])
            for att in igw_info["InternetGateways"][0].get("Attachments", []):
                ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=att["VpcId"])
        except Exception:
            pass
        ec2.delete_internet_gateway(InternetGatewayId=igw_id)
        return {"success": True, "action": "delete", "resource_type": "internet_gateway", "internet_gateway_id": igw_id, "message": f"Internet Gateway {igw_id} deleted"}

    async def _list_internet_gateway(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        response = ec2.describe_internet_gateways()
        igws = []
        for ig in response["InternetGateways"]:
            name = ""
            for t in ig.get("Tags", []):
                if t["Key"] == "Name":
                    name = t["Value"]
            attached = [a["VpcId"] for a in ig.get("Attachments", [])]
            igws.append({"id": ig["InternetGatewayId"], "name": name, "attached_vpcs": attached})
        return {"success": True, "action": "list", "resource_type": "internet_gateway", "internet_gateways": igws, "count": len(igws)}

    # ===================== Elastic IP =====================

    async def _create_eip(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        all_tags = {**(tags or {})}
        if resource_name:
            all_tags["Name"] = resource_name
        response = ec2.allocate_address(
            Domain="vpc",
            TagSpecifications=[{"ResourceType": "elastic-ip", "Tags": [{"Key": k, "Value": v} for k, v in all_tags.items()]}],
        )
        return {
            "success": True, "action": "create", "resource_type": "eip",
            "allocation_id": response["AllocationId"], "public_ip": response["PublicIp"], "name": resource_name,
        }

    async def _delete_eip(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        alloc_id = parameters.get("allocation_id") or resource_name
        if not alloc_id:
            return {"success": False, "error": "Allocation ID required for Elastic IP"}
        if not alloc_id.startswith("eipalloc-"):
            resp = ec2.describe_addresses(Filters=[{"Name": "tag:Name", "Values": [alloc_id]}])
            if resp["Addresses"]:
                alloc_id = resp["Addresses"][0]["AllocationId"]
        ec2.release_address(AllocationId=alloc_id)
        return {"success": True, "action": "delete", "resource_type": "eip", "allocation_id": alloc_id, "message": f"Elastic IP released"}

    async def _list_eip(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        ec2 = self._client("ec2")
        response = ec2.describe_addresses()
        eips = []
        for a in response["Addresses"]:
            name = ""
            for t in a.get("Tags", []):
                if t["Key"] == "Name":
                    name = t["Value"]
            eips.append({"allocation_id": a.get("AllocationId"), "public_ip": a.get("PublicIp"), "name": name, "instance_id": a.get("InstanceId"), "association_id": a.get("AssociationId")})
        return {"success": True, "action": "list", "resource_type": "eip", "elastic_ips": eips, "count": len(eips)}

    # ===================== CloudWatch Logs =====================

    async def _create_log_group(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        logs = self._client("logs")
        group_name = resource_name or f"/ai-platform/{uuid.uuid4().hex[:8]}"
        if not group_name.startswith("/"):
            group_name = f"/{group_name}"
        retention = parameters.get("retention_days", 30)
        logs.create_log_group(logGroupName=group_name, tags=tags or {})
        logs.put_retention_policy(logGroupName=group_name, retentionInDays=retention)
        return {"success": True, "action": "create", "resource_type": "log_group", "log_group_name": group_name, "retention_days": retention}

    async def _delete_log_group(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        if not resource_name:
            return {"success": False, "error": "Log group name required"}
        logs = self._client("logs")
        logs.delete_log_group(logGroupName=resource_name)
        return {"success": True, "action": "delete", "resource_type": "log_group", "log_group_name": resource_name, "message": f"Log group '{resource_name}' deleted"}

    async def _list_log_group(self, resource_name: str = None, parameters: Dict = None, tags: Dict = None) -> Dict:
        logs = self._client("logs")
        response = logs.describe_log_groups()
        groups = [{"name": g["logGroupName"], "retention": g.get("retentionInDays"), "stored_bytes": g.get("storedBytes", 0)} for g in response.get("logGroups", [])]
        return {"success": True, "action": "list", "resource_type": "log_group", "log_groups": groups, "count": len(groups)}
