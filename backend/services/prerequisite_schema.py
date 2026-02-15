"""
Dynamic prerequisite schema generator using botocore service models.
Builds follow-up questions per service operation instead of hardcoded forms.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import boto3


def _to_snake_case(name: str) -> str:
    if not name:
        return name
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", str(name))
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


class PrerequisiteSchemaService:
    """
    Auto-generates prerequisite questions by introspecting AWS operation models.
    """

    SERVICE_ALIASES: Dict[str, str] = {
        "ec2": "ec2",
        "security_group": "ec2",
        "subnet": "ec2",
        "nat_gateway": "ec2",
        "internet_gateway": "ec2",
        "eip": "ec2",
        "ebs": "ec2",
        "vpc": "ec2",
        "s3": "s3",
        "rds": "rds",
        "lambda": "lambda",
        "dynamodb": "dynamodb",
        "sns": "sns",
        "sqs": "sqs",
        "ecs": "ecs",
        "eks": "eks",
        "iam": "iam",
        "route53": "route53",
        "cloudfront": "cloudfront",
        "cloudwatch": "cloudwatch",
        "log_group": "logs",
        "secretsmanager": "secretsmanager",
        "ssm": "ssm",
        "kms": "kms",
        "acm": "acm",
        "apigateway": "apigateway",
        "stepfunctions": "stepfunctions",
        "elasticache": "elasticache",
        "kinesis": "kinesis",
        "ecr": "ecr",
        "elb": "elbv2",
        "waf": "wafv2",
        "redshift": "redshift",
        "emr": "emr",
        "spark": "emr",
        "sagemaker": "sagemaker",
        "glue": "glue",
        "athena": "athena",
        "codepipeline": "codepipeline",
        "codebuild": "codebuild",
        "wellarchitected": "wellarchitected",
    }

    RESOURCE_TOKENS: Dict[str, List[str]] = {
        "ec2": ["instance"],
        "security_group": ["securitygroup", "security_group"],
        "subnet": ["subnet"],
        "vpc": ["vpc"],
        "s3": ["bucket"],
        "rds": ["dbinstance", "db"],
        "lambda": ["function"],
        "dynamodb": ["table"],
        "sns": ["topic"],
        "sqs": ["queue"],
        "eks": ["cluster"],
        "ecs": ["cluster", "service"],
        "elb": ["loadbalancer"],
        "cloudfront": ["distribution"],
        "ecr": ["repository"],
        "route53": ["hostedzone", "record"],
        "elasticache": ["replicationgroup", "cachecluster"],
        "kinesis": ["stream"],
        "waf": ["webacl"],
        "redshift": ["cluster"],
        "emr": ["jobflow", "cluster"],
        "stepfunctions": ["statemachine"],
        "codepipeline": ["pipeline"],
        "codebuild": ["project"],
        "wellarchitected": ["workload", "lens", "profile", "reviewtemplate"],
    }

    OPERATION_OVERRIDES: Dict[Tuple[str, str, str], str] = {
        ("ec2", "ec2", "create"): "RunInstances",
        ("ec2", "ec2", "delete"): "TerminateInstances",
        ("ec2", "ec2", "list"): "DescribeInstances",
        ("ec2", "ec2", "describe"): "DescribeInstances",
        ("ec2", "security_group", "create"): "CreateSecurityGroup",
        ("ec2", "security_group", "delete"): "DeleteSecurityGroup",
        ("ec2", "security_group", "list"): "DescribeSecurityGroups",
        ("ec2", "security_group", "describe"): "DescribeSecurityGroups",
        ("ec2", "subnet", "create"): "CreateSubnet",
        ("ec2", "subnet", "delete"): "DeleteSubnet",
        ("ec2", "subnet", "list"): "DescribeSubnets",
        ("ec2", "subnet", "describe"): "DescribeSubnets",
        ("s3", "s3", "create"): "CreateBucket",
        ("s3", "s3", "delete"): "DeleteBucket",
        ("s3", "s3", "list"): "ListBuckets",
        ("s3", "s3", "describe"): "GetBucketLocation",
        ("rds", "rds", "create"): "CreateDBInstance",
        ("rds", "rds", "delete"): "DeleteDBInstance",
        ("rds", "rds", "list"): "DescribeDBInstances",
        ("rds", "rds", "describe"): "DescribeDBInstances",
        ("lambda", "lambda", "create"): "CreateFunction",
        ("lambda", "lambda", "delete"): "DeleteFunction",
        ("lambda", "lambda", "list"): "ListFunctions",
        ("lambda", "lambda", "describe"): "GetFunction",
        ("eks", "eks", "create"): "CreateCluster",
        ("eks", "eks", "delete"): "DeleteCluster",
        ("eks", "eks", "list"): "ListClusters",
        ("eks", "eks", "describe"): "DescribeCluster",
        ("emr", "emr", "create"): "RunJobFlow",
        ("emr", "emr", "delete"): "TerminateJobFlows",
        ("emr", "emr", "list"): "ListClusters",
        ("emr", "emr", "describe"): "DescribeCluster",
        ("elbv2", "elb", "create"): "CreateLoadBalancer",
        ("elbv2", "elb", "delete"): "DeleteLoadBalancer",
        ("elbv2", "elb", "list"): "DescribeLoadBalancers",
        ("elbv2", "elb", "describe"): "DescribeLoadBalancers",
        ("wafv2", "waf", "create"): "CreateWebACL",
        ("wafv2", "waf", "delete"): "DeleteWebACL",
        ("wafv2", "waf", "list"): "ListWebACLs",
        ("wafv2", "waf", "describe"): "GetWebACL",
        ("logs", "log_group", "create"): "CreateLogGroup",
        ("logs", "log_group", "delete"): "DeleteLogGroup",
        ("logs", "log_group", "list"): "DescribeLogGroups",
        ("logs", "log_group", "describe"): "DescribeLogGroups",
        ("wellarchitected", "wellarchitected", "create"): "CreateWorkload",
        ("wellarchitected", "wellarchitected", "delete"): "DeleteWorkload",
        ("wellarchitected", "wellarchitected", "list"): "ListWorkloads",
        ("wellarchitected", "wellarchitected", "describe"): "GetWorkload",
        ("wellarchitected", "wellarchitected", "update"): "UpdateWorkload",
    }

    FIELD_TO_VARIABLE: Dict[str, str] = {
        "ImageId": "ami_id",
        "InstanceType": "instance_type",
        "KeyName": "key_name",
        "SubnetId": "subnet_id",
        "SubnetIds": "subnet_ids",
        "SecurityGroupIds": "security_group_ids",
        "VpcId": "vpc_id",
        "CidrBlock": "cidr_block",
        "RoleArn": "role_arn",
        "roleArn": "role_arn",
        "Role": "role_arn",
        "ExecutionRoleArn": "role_arn",
        "ServiceRole": "role_arn",
        "serviceRole": "role_arn",
        "IamInstanceProfile": "iam_instance_profile",
        "UserData": "user_data",
        "DBInstanceClass": "instance_type",
        "DBName": "db_name",
        "DBInstanceIdentifier": "db_instance_id",
        "AllocatedStorage": "storage_size",
        "Engine": "engine",
        "EngineVersion": "engine_version",
        "MasterUsername": "master_username",
        "MasterUserPassword": "master_password",
        "PubliclyAccessible": "public_access",
        "ClusterIdentifier": "cluster_id",
        "ClusterName": "cluster_name",
        "FunctionName": "function_name",
        "Runtime": "runtime",
        "MemorySize": "memory",
        "Timeout": "timeout",
        "ReleaseLabel": "release_label",
        "MasterInstanceType": "master_instance_type",
        "SlaveInstanceType": "worker_instance_type",
        "InstanceCount": "instance_count",
        "Version": "kubernetes_version",
        "Name": "name",
    }

    EXCLUDED_FIELDS = {
        "DryRun",
        "ClientToken",
        "ClientRequestToken",
        "IdempotencyToken",
        "TagSpecifications",
        "Tags",
    }

    DEFAULTS_BY_FIELD: Dict[str, Any] = {
        "MinCount": 1,
        "MaxCount": 1,
    }

    CURATED_OPTIONS: Dict[str, List[Any]] = {
        "instance_type": ["t3.micro", "t3.small", "t3.medium", "m5.large", "c6i.large"],
        "os_flavor": ["amazon-linux-2", "amazon-linux-2023", "ubuntu-22.04", "ubuntu-20.04", "windows-2022"],
        "engine": ["postgres", "mysql", "mariadb", "aurora-postgresql"],
        "kubernetes_version": ["1.29", "1.30", "1.31"],
    }

    FRIENDLY_LABELS: Dict[str, str] = {
        "ami_id": "AMI ID",
        "os_flavor": "Operating system",
        "instance_type": "Instance type",
        "storage_size": "Storage size (GB)",
        "public_access": "Public access",
    }

    NETWORK_FIELD_NAMES = {
        "VpcId",
        "SubnetId",
        "SubnetIds",
        "SecurityGroupIds",
        "VpcSecurityGroupIds",
        "CidrBlock",
        "IpPermissions",
        "resourcesVpcConfig",
        "ResourcesVpcConfig",
    }

    NETWORK_PARAM_KEYS = {
        "vpc_id",
        "subnet_id",
        "subnet_ids",
        "security_group_id",
        "security_group_ids",
        "vpc_security_group_ids",
        "cidr_block",
        "ip_permissions",
        "resources_vpc_config",
        "resourcesVpcConfig",
    }

    def __init__(self):
        self._schema_session = boto3.session.Session(
            aws_access_key_id="x",
            aws_secret_access_key="x",
            region_name="us-east-1",
        )
        self._service_model_cache: Dict[str, Any] = {}

    def build_questions(self, intent: Dict[str, Any]) -> List[Dict[str, Any]]:
        intent = intent or {}
        action = str(intent.get("action") or "").strip().lower()
        resource_type = str(intent.get("resource_type") or "").strip().lower()
        params = intent.get("parameters", {}) if isinstance(intent.get("parameters"), dict) else {}
        region = intent.get("region") or "us-east-1"

        if action not in {"create", "update", "delete", "list", "describe"} or not resource_type:
            return []
        if action in {"list", "describe"}:
            return []

        service_name = self.SERVICE_ALIASES.get(resource_type, resource_type)
        operation_name = self._resolve_operation_name(service_name, resource_type, action)
        if not operation_name:
            return []

        operation_model = self._get_operation_model(service_name, operation_name, region)
        if not operation_model:
            return []

        return self._questions_from_operation_model(
            operation_model=operation_model,
            service_name=service_name,
            operation_name=operation_name,
            intent=intent,
            params=params,
        )

    def _resolve_operation_name(self, service_name: str, resource_type: str, action: str) -> Optional[str]:
        override = self.OPERATION_OVERRIDES.get((service_name, resource_type, action))
        if override and self._operation_exists(service_name, override):
            return override

        model = self._get_service_model(service_name)
        if not model:
            return None

        verbs = {
            "create": ["Create", "Run", "Put", "Start"],
            "update": ["Update", "Modify", "Put", "Associate", "Attach", "Enable"],
            "delete": ["Delete", "Remove", "Terminate", "Stop", "Disable", "Detach"],
            "list": ["List", "Describe", "Get"],
            "describe": ["Describe", "Get", "List"],
        }.get(action, ["Create"])

        tokens = self.RESOURCE_TOKENS.get(resource_type, [resource_type.replace("-", ""), resource_type.replace("_", "")])
        best_name = None
        best_score = -1

        for op_name in model.operation_names:
            score = self._score_operation(op_name, verbs, tokens)
            if score > best_score:
                best_score = score
                best_name = op_name

        return best_name if best_score >= 0 else None

    def _score_operation(self, operation_name: str, verbs: List[str], tokens: List[str]) -> int:
        score = -1
        for idx, prefix in enumerate(verbs):
            if operation_name.startswith(prefix):
                score = max(score, 100 - (idx * 10))
                break
        if score < 0:
            return -1

        lower_name = operation_name.lower()
        for token in tokens:
            t = str(token or "").replace("_", "").replace("-", "").lower()
            if t and t in lower_name:
                score += 8
        if "tag" in lower_name or "policy" in lower_name:
            score -= 4
        return score

    def _get_service_model(self, service_name: str):
        if service_name in self._service_model_cache:
            return self._service_model_cache[service_name]
        try:
            client = self._schema_session.client(service_name, region_name="us-east-1")
            model = client.meta.service_model
        except Exception:
            model = None
        self._service_model_cache[service_name] = model
        return model

    def _operation_exists(self, service_name: str, operation_name: str) -> bool:
        model = self._get_service_model(service_name)
        if not model:
            return False
        return operation_name in set(model.operation_names)

    def _get_operation_model(self, service_name: str, operation_name: str, region: str):
        try:
            client = self._schema_session.client(service_name, region_name=region or "us-east-1")
            return client.meta.service_model.operation_model(operation_name)
        except Exception:
            return None

    def _questions_from_operation_model(
        self,
        operation_model: Any,
        service_name: str,
        operation_name: str,
        intent: Dict[str, Any],
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        questions: List[Dict[str, Any]] = []
        input_shape = operation_model.input_shape
        if input_shape is None:
            return questions
        custom_networking = self._wants_custom_networking(params)

        required = list(getattr(input_shape, "required_members", []) or [])
        selected_fields = list(required)

        # Add high-impact optional fields when available.
        optional_candidates = ["InstanceType", "ImageId", "AllocatedStorage", "Engine", "DBInstanceClass", "PubliclyAccessible"]
        # For EC2 RunInstances, networking identifiers are auto-discovered by executor.
        # Do not ask VPC/Subnet unless user explicitly chooses custom networking path.
        if not (service_name == "ec2" and operation_name == "RunInstances"):
            optional_candidates.extend(["VpcId", "SubnetId", "SubnetIds"])
        for candidate in optional_candidates:
            if candidate in input_shape.members and candidate not in selected_fields:
                selected_fields.append(candidate)

        for field_name in selected_fields:
            if field_name in self.EXCLUDED_FIELDS:
                continue
            if field_name in self.DEFAULTS_BY_FIELD:
                continue
            if self._is_auto_managed_role_field(
                service_name=service_name,
                operation_name=operation_name,
                field_name=field_name,
                params=params,
            ):
                continue
            if service_name == "lambda" and operation_name == "CreateFunction" and field_name in {"Role", "Code", "Handler", "Publish", "PackageType"}:
                continue
            if self._is_network_field(field_name) and not custom_networking:
                continue

            shape = input_shape.members.get(field_name)
            if shape is None:
                continue

            variable = self.FIELD_TO_VARIABLE.get(field_name, _to_snake_case(field_name))
            if self._is_value_present(variable, field_name, intent, params):
                continue

            # Avoid asking for names that are already inferred from resource_name.
            if field_name in {"Bucket", "QueueName", "TopicName", "TableName", "FunctionName", "RoleName", "DBInstanceIdentifier", "ClusterIdentifier", "Name"}:
                if intent.get("resource_name"):
                    continue

            # Expand required nested structures into concrete child questions when possible.
            if str(getattr(shape, "type_name", "")) == "structure":
                if service_name == "eks" and operation_name == "CreateCluster" and field_name == "resourcesVpcConfig":
                    # Auto-discovered in executor prerequisite engine; only ask later if auto-discovery fails.
                    continue
                nested = self._expand_required_structure(
                    structure_shape=shape,
                    parent_field=field_name,
                    service_name=service_name,
                    operation_name=operation_name,
                    params=params,
                    custom_networking=custom_networking,
                )
                if nested:
                    questions.extend(nested)
                    continue

            qtype, hint = self._derive_type_and_hint(shape, field_name)
            question: Dict[str, Any] = {
                "variable": variable,
                "question": self._build_question_text(variable, field_name, qtype),
                "type": qtype,
                "source": {
                    "service": service_name,
                    "operation": operation_name,
                    "field": field_name,
                    "required": field_name in required,
                },
            }
            if hint:
                question["hint"] = hint

            options = self._derive_options(shape, variable)
            if options:
                question["options"] = options

            questions.append(question)

        # Layer practical compute prompts for EC2 UX.
        if service_name == "ec2" and operation_name == "RunInstances":
            self._append_ec2_extras(questions, params)
        if service_name == "lambda" and operation_name == "CreateFunction":
            self._append_lambda_extras(questions, params)

        return self._dedupe_questions(questions)

    def _is_auto_managed_role_field(
        self,
        service_name: str,
        operation_name: str,
        field_name: str,
        params: Dict[str, Any],
    ) -> bool:
        auto_manage = str(params.get("auto_manage_roles", "true")).strip().lower() not in {"false", "0", "no", "n"}
        if not auto_manage:
            return False

        normalized = str(field_name or "").strip().lower()
        if normalized not in {"role", "rolearn", "executionrolearn", "servicerole"}:
            return False

        op = str(operation_name or "").strip().lower()
        # Never ask end users to manually provide role ARNs for create/update style workflows.
        return op.startswith(("create", "run", "put", "update", "modify", "start"))

    def _expand_required_structure(
        self,
        structure_shape: Any,
        parent_field: str,
        service_name: str,
        operation_name: str,
        params: Dict[str, Any],
        custom_networking: bool,
    ) -> List[Dict[str, Any]]:
        nested_questions: List[Dict[str, Any]] = []
        if self._is_network_field(parent_field) and not custom_networking:
            return nested_questions
        required_children = list(getattr(structure_shape, "required_members", []) or [])
        if not required_children:
            return nested_questions

        for child_name in required_children:
            child_shape = structure_shape.members.get(child_name)
            if child_shape is None:
                continue
            if self._is_network_field(child_name) and not custom_networking:
                continue

            variable = self.FIELD_TO_VARIABLE.get(child_name, _to_snake_case(child_name))
            if variable in params and params.get(variable) not in (None, "", []):
                continue
            if child_name in params and params.get(child_name) not in (None, "", []):
                continue

            qtype, hint = self._derive_type_and_hint(child_shape, child_name)
            nested_question: Dict[str, Any] = {
                "variable": variable,
                "question": f"Please provide {self.FRIENDLY_LABELS.get(variable, child_name)} for {parent_field}.",
                "type": qtype,
                "source": {
                    "service": service_name,
                    "operation": operation_name,
                    "field": f"{parent_field}.{child_name}",
                    "required": True,
                },
            }
            if hint:
                nested_question["hint"] = hint
            options = self._derive_options(child_shape, variable)
            if options:
                nested_question["options"] = options
            nested_questions.append(nested_question)

        return nested_questions

    def _is_network_field(self, field_name: str) -> bool:
        field = str(field_name or "").strip()
        if not field:
            return False
        if field in self.NETWORK_FIELD_NAMES:
            return True
        normalized = field.lower()
        return any(token in normalized for token in ("subnet", "vpc", "securitygroup", "ippermission", "cidr", "network"))

    def _wants_custom_networking(self, params: Dict[str, Any]) -> bool:
        if not isinstance(params, dict):
            return False
        for flag in ("use_custom_networking", "custom_networking", "custom_network", "network_custom"):
            val = params.get(flag)
            if isinstance(val, bool) and val:
                return True
            if isinstance(val, str) and val.strip().lower() in {"true", "1", "yes", "y", "custom", "manual"}:
                return True

        profile = str(params.get("network_profile", "")).strip().lower()
        if profile in {"custom", "manual", "advanced"}:
            return True

        for key in self.NETWORK_PARAM_KEYS:
            value = params.get(key)
            if value in (None, "", []):
                continue
            if isinstance(value, str) and value.strip().lower() in {"default", "auto", "automatic", "none", "null"}:
                continue
            return True
        return False

    def _is_value_present(self, variable: str, aws_field: str, intent: Dict[str, Any], params: Dict[str, Any]) -> bool:
        if variable in params and params.get(variable) not in (None, "", []):
            return True
        if aws_field in params and params.get(aws_field) not in (None, "", []):
            return True
        if variable in {"name", "bucket", "table_name", "queue_name"} and intent.get("resource_name"):
            return True
        return False

    def _derive_type_and_hint(self, shape: Any, field_name: str) -> Tuple[str, Optional[str]]:
        type_name = str(getattr(shape, "type_name", "string"))
        if type_name in {"integer", "long", "double", "float"}:
            return "number", None
        if type_name == "boolean":
            return "boolean", "true or false"
        if type_name == "list":
            member = getattr(shape, "member", None)
            member_type = getattr(member, "type_name", "string") if member else "string"
            return "string", f"Comma-separated values ({member_type} list)"
        if type_name == "structure":
            return "string", "Provide JSON object"
        if field_name.lower().endswith("password"):
            return "password", None
        return "string", None

    def _derive_options(self, shape: Any, variable: str) -> Optional[List[Any]]:
        enum_values = list(getattr(shape, "enum", []) or [])
        if enum_values and len(enum_values) <= 12:
            return enum_values
        return self.CURATED_OPTIONS.get(variable)

    def _build_question_text(self, variable: str, field_name: str, qtype: str) -> str:
        label = self.FRIENDLY_LABELS.get(variable, field_name)
        if qtype == "boolean":
            return f"Should I enable {label.lower()}?"
        if qtype == "number":
            return f"What value should I use for {label}?"
        return f"Please provide {label}."

    def _append_ec2_extras(self, questions: List[Dict[str, Any]], params: Dict[str, Any]):
        existing = {q.get("variable") for q in questions}
        extras = [
            {
                "variable": "os_flavor",
                "question": "Choose operating system.",
                "type": "string",
                "options": self.CURATED_OPTIONS["os_flavor"],
            },
            {
                "variable": "storage_size",
                "question": "Choose root disk size (GB).",
                "type": "number",
                "hint": "Example: 20",
            },
            {
                "variable": "public_access",
                "question": "Should this instance be publicly accessible?",
                "type": "boolean",
                "options": ["true", "false"],
            },
        ]
        for item in extras:
            var = item["variable"]
            if var in existing:
                continue
            if var in params and params.get(var) not in (None, "", []):
                continue
            questions.append(item)

    def _append_lambda_extras(self, questions: List[Dict[str, Any]], params: Dict[str, Any]):
        existing = {q.get("variable") for q in questions}
        extras = [
            {
                "variable": "runtime",
                "question": "Choose Lambda runtime.",
                "type": "string",
                "options": ["python3.12", "nodejs20.x", "java21", "dotnet8", "go1.x"],
            },
            {
                "variable": "memory",
                "question": "Choose Lambda memory (MB).",
                "type": "number",
                "hint": "Example: 256",
            },
            {
                "variable": "timeout",
                "question": "Choose Lambda timeout (seconds).",
                "type": "number",
                "hint": "Example: 30",
            },
        ]
        for item in extras:
            var = item["variable"]
            if var in existing:
                continue
            if var in params and params.get(var) not in (None, "", []):
                continue
            questions.append(item)

    def _dedupe_questions(self, questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        deduped: List[Dict[str, Any]] = []
        for q in questions:
            var = q.get("variable")
            if not var or var in seen:
                continue
            seen.add(var)
            deduped.append(q)
        return deduped
