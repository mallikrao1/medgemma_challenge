"""
Advanced NLU Service - Extracts intent, entities, and parameters from natural language.
Uses LLM for understanding with robust fallback parsing.
Supports ALL AWS services dynamically.
"""

import re
import json
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from config import settings
from services.model_router import ModelRouter

try:
    import ollama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False


@dataclass
class ParsedIntent:
    action: str  # create, update, delete, list, describe
    resource_type: str  # s3, ec2, rds, lambda, vpc, iam, dynamodb, sns, sqs, etc.
    resource_name: Optional[str] = None
    region: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    raw_llm_response: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_name": self.resource_name,
            "region": self.region,
            "parameters": self.parameters,
            "confidence": self.confidence,
        }


class NLUService:
    def __init__(self):
        self.model = settings.NLU_PRIMARY_MODEL or settings.OLLAMA_ORCHESTRATOR_MODEL
        self.model_router = ModelRouter()

        self.action_patterns = {
            "create": ["create", "make", "build", "provision", "deploy", "launch", "setup", "add", "spin up", "start", "new"],
            "delete": ["delete", "remove", "destroy", "terminate", "kill", "drop", "tear down", "shut down", "decommission"],
            "update": ["update", "modify", "change", "edit", "alter", "resize", "scale", "upgrade", "downgrade", "reconfigure"],
            "list": ["list", "show all", "display all", "get all", "find all", "enumerate"],
            "describe": ["describe", "show", "get", "details", "info", "status", "check"],
        }

        self.resource_patterns = {
            "s3": ["s3", "bucket", "storage bucket", "object storage", "s3 bucket"],
            "lambda": ["lambda", "function", "serverless", "serverless function", "lambda function"],
            "ec2": ["ec2", "instance", "virtual machine", "vm", "server", "compute"],
            "rds": ["rds", "database", "mysql", "postgres", "postgresql", "mariadb", "aurora", "db instance", "sql server", "oracle db"],
            "vpc": ["vpc", "virtual private cloud", "network", "virtual network", "subnet", "subnets"],
            "iam": ["iam", "role", "policy", "permissions", "iam role", "iam user", "iam policy"],
            "elb": ["load balancer", "elb", "alb", "nlb", "application load balancer", "network load balancer"],
            "cloudwatch": ["alarm", "cloudwatch", "monitoring", "alert", "metric", "log group", "cloudwatch alarm"],
            "dynamodb": ["dynamodb", "dynamo", "nosql", "dynamo table", "dynamodb table"],
            "sns": ["sns", "notification", "topic", "sns topic", "push notification"],
            "sqs": ["sqs", "queue", "message queue", "sqs queue"],
            "ecs": ["ecs", "container", "fargate", "ecs cluster", "ecs service", "container service"],
            "eks": ["eks", "kubernetes", "k8s", "eks cluster", "kubernates", "kubernets", "kubernete"],
            "route53": ["route53", "dns", "domain", "hosted zone", "dns record"],
            "cloudfront": ["cloudfront", "cdn", "distribution", "content delivery"],
            "elasticache": ["elasticache", "redis cluster", "memcached", "cache cluster"],
            "kinesis": ["kinesis", "stream", "data stream", "kinesis stream"],
            "secretsmanager": ["secret", "secrets manager", "secretsmanager"],
            "ssm": ["ssm", "parameter store", "systems manager", "parameter"],
            "ecr": ["ecr", "container registry", "docker registry"],
            "stepfunctions": ["step function", "step functions", "state machine", "workflow"],
            "apigateway": ["api gateway", "apigateway", "rest api", "http api"],
            "codepipeline": ["codepipeline", "pipeline", "ci/cd", "cicd"],
            "codebuild": ["codebuild", "build project"],
            "glue": ["glue", "etl", "data catalog", "crawler"],
            "athena": ["athena", "query", "sql query"],
            "redshift": ["redshift", "data warehouse", "warehouse"],
            "emr": ["emr", "spark", "hadoop", "big data cluster"],
            "sagemaker": ["sagemaker", "ml", "machine learning", "notebook", "training job"],
            "security_group": ["security group", "firewall", "sg", "inbound rule", "outbound rule"],
            "ebs": ["ebs", "volume", "block storage", "ebs volume"],
            "efs": ["efs", "file system", "elastic file system"],
            "acm": ["acm", "certificate", "ssl", "tls", "ssl certificate"],
            "kms": ["kms", "key", "encryption key", "kms key"],
            "waf": ["waf", "web application firewall", "firewall rule"],
            "wellarchitected": ["well architected", "well-architected", "wellarchitected", "workload review"],
        }
        self.resource_rank_bias = {
            "vpc": 5,
            "elb": 4,
            "apigateway": 4,
            "lambda": 4,
            "ecs": 4,
            "eks": 4,
            "rds": 3,
            "s3": 3,
            "security_group": 3,
            "waf": 3,
            "glue": 3,
            "athena": 2,
            "ec2": 1,
        }

    async def parse_request(self, natural_language: str, user_region: Optional[str] = None) -> ParsedIntent:
        """Parse natural language into structured intent using LLM with fallback."""
        print(f"  NLU Parsing: {natural_language}")

        if OLLAMA_AVAILABLE:
            try:
                llm_intent = await self._llm_parse(natural_language, user_region)
                enriched_intent = self._enrich_intent(llm_intent, natural_language, user_region)
                return await self._verify_intent(enriched_intent, natural_language, user_region)
            except Exception as e:
                print(f"  LLM parsing failed: {e}, using fallback")

        fallback_intent = self._fallback_parse(natural_language, user_region)
        return self._enrich_intent(fallback_intent, natural_language, user_region)

    async def _llm_parse(self, natural_language: str, user_region: Optional[str] = None) -> ParsedIntent:
        """Use LLM to extract structured intent from natural language."""
        prompt = f"""You are an AWS infrastructure intent parser. Extract EXACT information from the user's request.

CRITICAL RULES:
- Extract the EXACT resource name the user specified. Do NOT generate a default name.
- If user says "create S3 bucket with name mallik", resource_name MUST be "mallik"
- If user says "create S3 bucket named my-data", resource_name MUST be "my-data"
- If user says "delete bucket test-bucket", resource_name MUST be "test-bucket"
- If no name is mentioned, set resource_name to null

Request: "{natural_language}"

Respond with ONLY valid JSON (no markdown, no explanation):
{{
  "action": "create|update|delete|list|describe",
  "resource_type": "s3|ec2|rds|lambda|vpc|iam|elb|cloudwatch|dynamodb|sns|sqs|ecs|eks|route53|cloudfront|elasticache|kinesis|secretsmanager|ssm|ecr|stepfunctions|apigateway|security_group|ebs|efs|acm|kms|waf|redshift|emr|sagemaker|glue|athena|codepipeline|codebuild|wellarchitected",
  "resource_name": "exact-name-from-request-or-null",
  "region": "aws-region-or-null",
  "parameters": {{}}
}}

For parameters, extract any details like:
- instance_type (t3.micro, t3.small, m5.large, etc.)
- storage_size, encryption, versioning, public_access
- engine (mysql, postgres, etc.), engine_version
- runtime (python3.9, nodejs18.x, etc.)
- cidr_block, port, protocol
- key_name, ami_id
- If request says serverless/server less/server-less, prefer resource_type=lambda (or apigateway if explicitly API-focused), not ec2
- Any other configuration the user mentions

JSON:"""

        response = self.model_router.generate(
            task="nlu",
            prompt=prompt,
            options={"temperature": 0},
            format="json",
        )
        raw = response["response"].strip()

        # Clean markdown fences
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()

        # Find JSON object
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            raw = raw[start:end]

        parsed = json.loads(raw)

        if user_region:
            parsed["region"] = user_region

        intent = ParsedIntent(
            action=parsed.get("action", "create").lower().strip(),
            resource_type=parsed.get("resource_type", "unknown").lower().strip(),
            resource_name=parsed.get("resource_name"),
            region=parsed.get("region") or user_region or "us-east-1",
            parameters=parsed.get("parameters", {}),
            confidence=0.95,
            raw_llm_response=raw,
        )

        print(f"  LLM Parsed: action={intent.action}, type={intent.resource_type}, name={intent.resource_name}")
        return intent

    async def _verify_intent(
        self,
        intent: ParsedIntent,
        natural_language: str,
        user_region: Optional[str] = None,
    ) -> ParsedIntent:
        if not OLLAMA_AVAILABLE or not settings.NLU_ENABLE_VERIFIER:
            return intent

        verification_prompt = f"""You verify AWS intent parsing quality.
User request: "{natural_language}"
Parsed intent JSON:
{json.dumps(intent.to_dict(), ensure_ascii=False)}

Rules:
- If parsed intent is correct, keep it.
- If resource mapping is wrong, fix it.
- Pay special attention to serverless/server less/server-less phrasing (must not map to EC2 by default).
- Return ONLY JSON:
{{
  "is_valid": true,
  "confidence": 0.0,
  "action": "create|update|delete|list|describe",
  "resource_type": "aws resource key",
  "resource_name": null,
  "region": null,
  "parameters": {{}},
  "reason": "short reason"
}}"""
        try:
            response = self.model_router.generate(
                task="intent_verifier",
                prompt=verification_prompt,
                options={"temperature": 0},
                format="json",
            )
            raw = response.get("response", "").strip()
            parsed = self._parse_json_payload(raw)
            if not isinstance(parsed, dict):
                return intent

            verifier_conf = parsed.get("confidence", 0.0)
            try:
                verifier_conf = float(verifier_conf)
            except Exception:
                verifier_conf = 0.0
            if verifier_conf < settings.NLU_VERIFIER_MIN_CONFIDENCE:
                return intent

            action = str(parsed.get("action") or intent.action).lower().strip()
            if action not in self.action_patterns:
                action = intent.action

            resource_type = str(parsed.get("resource_type") or intent.resource_type).lower().strip()
            if resource_type not in self.resource_patterns:
                resource_type = intent.resource_type

            candidate = ParsedIntent(
                action=action,
                resource_type=resource_type,
                resource_name=parsed.get("resource_name") if not self._is_placeholder_value(parsed.get("resource_name")) else intent.resource_name,
                region=parsed.get("region") or user_region or intent.region,
                parameters=parsed.get("parameters") if isinstance(parsed.get("parameters"), dict) else intent.parameters,
                confidence=max(intent.confidence, verifier_conf),
                raw_llm_response=intent.raw_llm_response,
            )
            verified = self._enrich_intent(candidate, natural_language, user_region)
            print(f"  Intent verifier accepted: action={verified.action}, type={verified.resource_type}")
            return verified
        except Exception as e:
            print(f"  Intent verifier skipped: {e}")
            return intent

    def _fallback_parse(self, text: str, user_region: Optional[str] = None) -> ParsedIntent:
        """Robust fallback parser using regex patterns."""
        text_lower = text.lower().strip()
        is_serverless = self._is_serverless_phrase(text_lower)

        # Detect action
        action = "create"
        for act, keywords in self.action_patterns.items():
            if any(kw in text_lower for kw in keywords):
                action = act
                break

        # Detect resource type (ranked for all-services support).
        inferred_targets = self._infer_service_targets(text_lower)
        resource_type = inferred_targets[0] if inferred_targets else ("lambda" if is_serverless else "unknown")

        # Extract resource name with multiple patterns
        resource_name = self._extract_resource_name(text, text_lower)

        # Detect region
        region = user_region
        region_match = re.search(
            r'(us-east-[12]|us-west-[12]|eu-west-[123]|eu-central-1|eu-north-1|'
            r'ap-south-[12]|ap-southeast-[123]|ap-northeast-[123]|'
            r'sa-east-1|ca-central-1|me-south-1|af-south-1)',
            text_lower
        )
        if region_match:
            region = region_match.group(1)

        # Extract parameters
        parameters = self._extract_parameters(text_lower)
        if inferred_targets:
            parameters.setdefault("service_targets", inferred_targets)
        architecture_style = self._derive_architecture_style(text_lower, inferred_targets)
        if architecture_style:
            parameters.setdefault("architecture_style", architecture_style)

        return ParsedIntent(
            action=action,
            resource_type=resource_type,
            resource_name=resource_name,
            region=region or "us-east-1",
            parameters=parameters,
            confidence=0.75,
        )

    def _extract_resource_name(self, text: str, text_lower: str) -> Optional[str]:
        """Extract the exact resource name from natural language."""
        # Pattern: "named X" or "called X" or "name X"
        match = re.search(r'(?:named|called|name)\s+["\']?([a-zA-Z0-9][\w.-]*)["\']?', text_lower)
        if match:
            return match.group(1)

        # Pattern: "with name X"
        match = re.search(r'with\s+name\s+["\']?([a-zA-Z0-9][\w.-]*)["\']?', text_lower)
        if match:
            return match.group(1)

        # Pattern: Quoted names
        match = re.search(r'["\']([a-zA-Z0-9][\w.-]+)["\']', text)
        if match:
            return match.group(1)

        # Pattern: "bucket X" or "instance X" where X looks like a name
        match = re.search(
            r'(?:bucket|instance|table|queue|topic|function|cluster|role|policy|group|domain|stream|secret|key|volume|certificate)\s+'
            r'([a-zA-Z][a-zA-Z0-9_-]+)',
            text_lower
        )
        if match:
            name = match.group(1)
            # Filter out action words and common prepositions
            stop_words = {"in", "on", "at", "for", "with", "from", "to", "the", "and", "or",
                          "named", "called", "using", "that", "this", "which", "type", "size",
                          "create", "delete", "update", "list", "describe"}
            if name not in stop_words and len(name) > 1:
                return name

        return None

    def _extract_parameters(self, text_lower: str) -> Dict[str, Any]:
        """Extract configuration parameters from the text."""
        params = {}
        if self._is_serverless_phrase(text_lower):
            params["architecture_style"] = "serverless"
            params.setdefault("use_api_gateway", True)
        if "fargate" in text_lower:
            params["architecture_style"] = "container_fargate"
        if any(token in text_lower for token in ["api gateway", "apigateway", "http api", "rest api"]):
            params.setdefault("use_api_gateway", True)
        if any(token in text_lower for token in ["glue", "etl", "crawler", "data catalog"]):
            params.setdefault("architecture_style", "data_pipeline")
        if any(token in text_lower for token in ["firewall", "waf", "security group", "ingress", "egress"]):
            params.setdefault("architecture_style", "security_hardening")

        # Instance type
        match = re.search(r'(t[23]\.\w+|m[456]\.\w+|c[567]\.\w+|r[567]\.\w+|p[234]\.\w+|g[45]\.\w+)', text_lower)
        if match:
            params["instance_type"] = match.group(1)

        # OS flavor (EC2 images)
        if any(x in text_lower for x in ["amazon linux 2023", "al2023", "amazonlinux2023"]):
            params["os_flavor"] = "amazon-linux-2023"
        elif any(x in text_lower for x in ["amazon linux 2", "amzn2", "amazonlinux2"]):
            params["os_flavor"] = "amazon-linux-2"
        elif any(x in text_lower for x in ["ubuntu 22", "ubuntu22", "jammy"]):
            params["os_flavor"] = "ubuntu-22.04"
        elif any(x in text_lower for x in ["ubuntu 20", "ubuntu20", "focal"]):
            params["os_flavor"] = "ubuntu-20.04"
        elif any(x in text_lower for x in ["windows 2022", "windows server 2022"]):
            params["os_flavor"] = "windows-2022"

        # Storage size
        match = re.search(r'(\d+)\s*(?:gb|gib|tb|tib)', text_lower)
        if match:
            params["storage_size"] = int(match.group(1))

        # Encryption
        if any(w in text_lower for w in ["encrypt", "encrypted", "encryption"]):
            params["encryption"] = True

        # Versioning
        if any(w in text_lower for w in ["version", "versioning"]):
            params["versioning"] = True

        # S3 static website hints
        if any(w in text_lower for w in ["website_configuration", "website configuration", "static website", "host website", "website hosting"]):
            params["website_configuration"] = True
        index_match = re.search(r'index(?:[_\s-]?document)?\s*(?:=|:|is)?\s*([a-z0-9._/-]+\.html?)', text_lower)
        if index_match:
            params["index_document"] = index_match.group(1)
            params.setdefault("website_configuration", True)
        error_match = re.search(r'error(?:[_\s-]?document)?\s*(?:=|:|is)?\s*([a-z0-9._/-]+\.html?)', text_lower)
        if error_match:
            params["error_document"] = error_match.group(1)
            params.setdefault("website_configuration", True)

        # Public access
        if "public" in text_lower:
            params["public_access"] = True
        if "private" in text_lower:
            params["public_access"] = False

        # Kubernetes website/public URL intent hints
        if any(token in text_lower for token in ["kubernetes", "k8s", "eks", "kubernates", "kubernets", "kubernete"]):
            params.setdefault("orchestrator", "kubernetes")
            if any(token in text_lower for token in ["website", "web app", "webapp", "frontend", "sample site"]):
                params.setdefault("deploy_sample_website", True)
            if any(token in text_lower for token in ["public", "internet", "url", "accessible"]):
                params.setdefault("expose_public_url", True)
            if any(token in text_lower for token in ["private worker", "private workers", "no public worker", "no public workers"]):
                params.setdefault("private_workers", True)
            node_count_match = re.search(r"(?:desired|nodes?|node count)\s*[:=]?\s*(\d+)", text_lower)
            if node_count_match:
                params.setdefault("node_count", int(node_count_match.group(1)))
        if "fargate" in text_lower:
            params.setdefault("launch_type", "FARGATE")
            if any(token in text_lower for token in ["website", "web app", "webapp", "api", "service"]):
                params.setdefault("requires_load_balancer", True)

        # Engine
        for engine in ["mysql", "postgres", "postgresql", "mariadb", "oracle", "sqlserver", "aurora"]:
            if engine in text_lower:
                params["engine"] = "postgresql" if engine == "postgres" else engine
                break

        # Runtime for Lambda
        for runtime in ["python3.9", "python3.10", "python3.11", "python3.12",
                        "nodejs16.x", "nodejs18.x", "nodejs20.x",
                        "java11", "java17", "java21",
                        "go1.x", "ruby3.2", "dotnet6", "dotnet8"]:
            if runtime.replace(".", "").replace("_", "") in text_lower.replace(".", "").replace("_", ""):
                params["runtime"] = runtime
                break

        # CIDR block
        match = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2})', text_lower)
        if match:
            params["cidr_block"] = match.group(1)

        # Port
        match = re.search(r'port\s+(\d+)', text_lower)
        if match:
            params["port"] = int(match.group(1))

        # Key pair
        match = re.search(r'key\s*(?:pair|name)\s+(\S+)', text_lower)
        if match:
            params["key_name"] = match.group(1).strip(" ,.;'\"")

        # AMI
        match = re.search(r'(ami-[a-f0-9]+)', text_lower)
        if match:
            params["ami_id"] = match.group(1)

        # OS package/app installation intent
        install_targets = []
        if any(word in text_lower for word in ["tomcat", "tomact"]):
            install_targets.append("tomcat")
            params.setdefault("port", 8080)
        if install_targets:
            params["install_targets"] = install_targets
            params["user_data"] = self._build_user_data(install_targets)

        return params

    def _enrich_intent(self, intent: ParsedIntent, text: str, user_region: Optional[str] = None) -> ParsedIntent:
        """Apply deterministic post-processing so model output is reliable."""
        text_lower = text.lower().strip()
        inferred_targets = self._infer_service_targets(text_lower)
        inferred_primary = inferred_targets[0] if inferred_targets else "unknown"
        fallback_intent = None
        action = intent.action
        if action not in self.action_patterns:
            fallback_intent = self._fallback_parse(text, user_region)
            action = fallback_intent.action

        resource_type = intent.resource_type
        if resource_type not in self.resource_patterns:
            resource_type = inferred_primary
            if resource_type not in self.resource_patterns:
                if fallback_intent is None:
                    fallback_intent = self._fallback_parse(text, user_region)
                resource_type = fallback_intent.resource_type
        if self._is_serverless_phrase(text_lower) and resource_type in {"ec2", "unknown"}:
            resource_type = "apigateway" if any(token in text_lower for token in ["api gateway", "http api", "rest api"]) else "lambda"
        if inferred_targets and self._is_architecture_level_request(text_lower):
            top_target = inferred_targets[0]
            if top_target in self.resource_patterns and (resource_type in {"unknown", "ec2"} or resource_type != top_target):
                resource_type = top_target
        if self._is_three_tier_phrase(text_lower) and resource_type in {"ec2", "unknown"}:
            # Multi-tier architecture requests should not collapse to plain EC2 instance flows.
            resource_type = "vpc"

        region = intent.region or user_region
        region_match = re.search(
            r'(us-east-[12]|us-west-[12]|eu-west-[123]|eu-central-1|eu-north-1|'
            r'ap-south-[12]|ap-southeast-[123]|ap-northeast-[123]|'
            r'sa-east-1|ca-central-1|me-south-1|af-south-1)',
            text_lower
        )
        if region_match:
            region = region_match.group(1)

        resource_name = intent.resource_name
        if self._is_placeholder_value(resource_name):
            resource_name = self._extract_resource_name(text, text_lower)
        resource_name = self._sanitize_resource_name(resource_name, resource_type)

        base_parameters = intent.parameters if isinstance(intent.parameters, dict) else {}
        heuristic_parameters = self._extract_parameters(text_lower)
        merged_parameters = dict(base_parameters)
        for key, value in heuristic_parameters.items():
            if key not in merged_parameters or self._is_placeholder_value(merged_parameters.get(key)):
                merged_parameters[key] = value

        merged_parameters = self._sanitize_parameters(merged_parameters)
        if inferred_targets:
            merged_parameters["service_targets"] = inferred_targets
        architecture_style = self._derive_architecture_style(text_lower, inferred_targets)
        if architecture_style:
            merged_parameters.setdefault("architecture_style", architecture_style)
        if self._is_serverless_phrase(text_lower):
            merged_parameters.setdefault("architecture_style", "serverless")
            merged_parameters.setdefault("use_api_gateway", True)
            if any(token in text_lower for token in ["web app", "webapp", "webapplication", "website"]):
                merged_parameters.setdefault("delivery_tier", "web")
        if self._is_three_tier_phrase(text_lower):
            merged_parameters.setdefault("architecture_style", "three_tier")
            merged_parameters.setdefault("requires_vpc", True)
            merged_parameters.setdefault("requires_alb", any(token in text_lower for token in ["alb", "load balancer"]))
            merged_parameters.setdefault("requires_private_app_tier", any(token in text_lower for token in ["private app tier", "private tier"]))
            merged_parameters.setdefault(
                "requires_database_tier",
                any(token in text_lower for token in ["rds", "database", "db tier"]),
            )
            merged_parameters.setdefault("requires_health_checks", "health check" in text_lower or "health checks" in text_lower)

        if "vpc_security_group_ids" in merged_parameters and "security_group_id" not in merged_parameters:
            sg_value = merged_parameters.get("vpc_security_group_ids")
            if isinstance(sg_value, list) and sg_value:
                merged_parameters["security_group_id"] = sg_value[0]
            elif isinstance(sg_value, str) and sg_value.strip():
                merged_parameters["security_group_id"] = sg_value.strip()

        if any(word in text_lower for word in ["tomcat", "tomact"]):
            merged_parameters["install_targets"] = ["tomcat"]
            merged_parameters["user_data"] = self._build_user_data(["tomcat"])
            merged_parameters.setdefault("port", 8080)
            merged_parameters.setdefault("wait_for_instance", True)

        return ParsedIntent(
            action=action,
            resource_type=resource_type,
            resource_name=resource_name,
            region=region or "us-east-1",
            parameters=merged_parameters,
            confidence=intent.confidence,
            raw_llm_response=intent.raw_llm_response,
        )

    def _sanitize_resource_name(self, resource_name: Optional[str], resource_type: str) -> Optional[str]:
        raw = str(resource_name or "").strip()
        if not raw:
            return None
        lowered = raw.lower()
        reserved_common = {
            "aws",
            "resource",
            "service",
            "cluster",
            "mode",
            "server",
            "instance",
            "database",
            "bucket",
            "function",
            "default",
            "new",
        }
        reserved_by_type = {
            "eks": {"eks", "kubernetes", "k8s", "kubenates", "kubernates", "kubernets", "kubernete"},
            "ecs": {"ecs", "fargate", "container"},
            "lambda": {"lambda", "serverless"},
            "apigateway": {"api", "apigateway", "api-gateway"},
            "s3": {"s3", "bucket"},
            "vpc": {"vpc", "network", "subnet"},
        }
        forbidden = set(reserved_common) | set(reserved_by_type.get(str(resource_type or "").lower(), set()))
        if lowered in forbidden:
            return None
        return raw

    def _is_architecture_level_request(self, text: str) -> bool:
        lowered = str(text or "").lower()
        return any(
            token in lowered
            for token in [
                "architecture",
                "build app",
                "web app",
                "web application",
                "website",
                "pipeline",
                "platform",
                "serverless",
                "fargate",
                "kubernetes",
                "3 tier",
                "three tier",
                "security setup",
                "firewall",
            ]
        )

    def _infer_service_targets(self, text: str) -> List[str]:
        lowered = str(text or "").lower()
        if not lowered.strip():
            return []

        scores: Dict[str, int] = {}
        for resource, keywords in self.resource_patterns.items():
            score = 0
            for kw in keywords:
                token = str(kw or "").strip().lower()
                if not token:
                    continue
                if " " in token:
                    if token in lowered:
                        score += 3
                elif re.search(rf"\b{re.escape(token)}\b", lowered):
                    score += 2
            if score:
                scores[resource] = score + int(self.resource_rank_bias.get(resource, 0))

        # Architecture-aware boosts.
        if self._is_serverless_phrase(lowered):
            scores["lambda"] = scores.get("lambda", 0) + 8
            scores["apigateway"] = scores.get("apigateway", 0) + 6
            if any(token in lowered for token in ["website", "web app", "frontend", "static"]):
                scores["s3"] = scores.get("s3", 0) + 4
                scores["cloudfront"] = scores.get("cloudfront", 0) + 4
        if "fargate" in lowered:
            scores["ecs"] = scores.get("ecs", 0) + 10
            scores["ecr"] = scores.get("ecr", 0) + 3
            if any(token in lowered for token in ["public", "internet", "website", "web app", "api"]):
                scores["elb"] = scores.get("elb", 0) + 5
        if any(token in lowered for token in ["kubernetes", "k8s", "eks", "kubernates", "kubernets", "kubernete"]):
            scores["eks"] = scores.get("eks", 0) + 10
            scores["elb"] = scores.get("elb", 0) + 3
        if any(token in lowered for token in ["api gateway", "apigateway", "http api", "rest api"]):
            scores["apigateway"] = scores.get("apigateway", 0) + 10
        if any(token in lowered for token in ["glue", "etl", "crawler", "data catalog"]):
            scores["glue"] = scores.get("glue", 0) + 10
            scores["s3"] = scores.get("s3", 0) + 3
            scores["athena"] = scores.get("athena", 0) + 3
        if any(token in lowered for token in ["firewall", "waf", "web application firewall"]):
            scores["waf"] = scores.get("waf", 0) + 10
            scores["security_group"] = scores.get("security_group", 0) + 6
        if self._is_three_tier_phrase(lowered):
            scores["vpc"] = scores.get("vpc", 0) + 10
            scores["elb"] = scores.get("elb", 0) + 8
            scores["rds"] = scores.get("rds", 0) + 8
            if "fargate" in lowered:
                scores["ecs"] = scores.get("ecs", 0) + 6
            elif "kubernetes" in lowered or "eks" in lowered:
                scores["eks"] = scores.get("eks", 0) + 6
            else:
                scores["ec2"] = scores.get("ec2", 0) + 5

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        return [resource for resource, score in ranked if score >= 4][:6]

    def _derive_architecture_style(self, text: str, inferred_targets: List[str]) -> Optional[str]:
        lowered = str(text or "").lower()
        targets = set(inferred_targets or [])
        if self._is_serverless_phrase(lowered):
            if any(token in lowered for token in ["website", "web app", "frontend", "url", "public"]):
                return "serverless_web"
            return "serverless"
        if "fargate" in lowered or "ecs" in lowered:
            return "container_fargate"
        if "kubernetes" in lowered or "k8s" in lowered or "eks" in lowered:
            return "kubernetes"
        if self._is_three_tier_phrase(lowered):
            return "three_tier"
        if "glue" in lowered or "etl" in lowered or "crawler" in lowered:
            return "data_pipeline"
        if "firewall" in lowered or "waf" in lowered or "security group" in lowered:
            return "security_hardening"
        if {"lambda", "apigateway"} <= targets:
            return "serverless"
        return None

    def _is_serverless_phrase(self, text: str) -> bool:
        raw = (text or "").lower()
        condensed = re.sub(r"[^a-z0-9]", "", raw)
        if "serverless" in condensed:
            return True
        return bool(re.search(r"\bserver\s*[-]?\s*less\b", raw))

    def _is_three_tier_phrase(self, text: str) -> bool:
        raw = (text or "").lower()
        condensed = re.sub(r"[^a-z0-9]", "", raw)
        if "3tier" in condensed or "threetier" in condensed or "multitier" in condensed:
            return True
        return bool(re.search(r"\b(3|three)\s*[-]?\s*tier\b", raw))

    def _sanitize_parameters(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        sanitized = {}
        for key, value in parameters.items():
            if key is None:
                continue
            if self._is_placeholder_value(value):
                continue

            if isinstance(value, str):
                cleaned = value.strip().strip("'\"")
                if self._is_placeholder_value(cleaned):
                    continue
                if key == "user_data":
                    cleaned = self._sanitize_user_data(cleaned)
                    if not cleaned:
                        continue
                sanitized[key] = cleaned
                continue

            if isinstance(value, list):
                cleaned_list = []
                for item in value:
                    if self._is_placeholder_value(item):
                        continue
                    if isinstance(item, str):
                        item = item.strip().strip("'\"")
                        if self._is_placeholder_value(item):
                            continue
                    cleaned_list.append(item)
                if cleaned_list:
                    sanitized[key] = cleaned_list
                continue

            sanitized[key] = value

        return sanitized

    def _parse_json_payload(self, raw: str) -> Optional[Dict[str, Any]]:
        if not raw:
            return None
        cleaned = raw.strip()
        if "```json" in cleaned:
            cleaned = cleaned.split("```json")[1].split("```")[0].strip()
        elif "```" in cleaned:
            cleaned = cleaned.split("```")[1].split("```")[0].strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            cleaned = cleaned[start:end]
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
        return None

    def _is_placeholder_value(self, value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized in {"", "null", "none", "auto-generated", "default", "n/a", "na"}
        if isinstance(value, list):
            return len(value) == 0
        return False

    def _build_user_data(self, install_targets: List[str]) -> str:
        if "tomcat" not in install_targets:
            return ""
        return """#!/bin/bash
set -euxo pipefail
if command -v dnf >/dev/null 2>&1; then PM=dnf; else PM=yum; fi
$PM update -y
$PM install -y java-17-amazon-corretto
TOMCAT_VERSION=10.1.28
cd /opt
curl -fsSL -o apache-tomcat.tar.gz https://dlcdn.apache.org/tomcat/tomcat-10/v${TOMCAT_VERSION}/bin/apache-tomcat-${TOMCAT_VERSION}.tar.gz
tar -xzf apache-tomcat.tar.gz
ln -sfn apache-tomcat-${TOMCAT_VERSION} tomcat
chmod +x /opt/tomcat/bin/*.sh
nohup /opt/tomcat/bin/startup.sh >/var/log/tomcat-start.log 2>&1 &
"""

    def _sanitize_user_data(self, user_data: str) -> str:
        """Remove obvious placeholder noise lines that break scripts."""
        lines = user_data.replace("\r\n", "\n").replace("\r", "\n").split("\n")
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
