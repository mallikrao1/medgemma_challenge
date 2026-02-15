#!/usr/bin/env python3
"""
Deploy MedGemma Challenge app to AWS App Runner using boto3 + Docker.

Prerequisites:
- Docker Desktop running
- AWS credentials configured (env vars or ~/.aws)
- ECR/AppRunner/IAM permissions
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError


ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = Path(__file__).resolve().parent


def run(cmd: list[str], check: bool = True, display_cmd: str | None = None) -> subprocess.CompletedProcess:
    shown = display_cmd if display_cmd is not None else " ".join(cmd)
    print("+", shown)
    return subprocess.run(cmd, check=check, text=True)


def sanitize_name(value: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9-]+", "-", value).strip("-").lower()
    if not cleaned:
        cleaned = "medgemma-challenge"
    return cleaned[:max_len]


@dataclass
class DeployConfig:
    app_name: str
    region: str
    repository: str
    service_name: str
    image_tag: str
    model_backend: str


def verify_aws_access(region: str) -> tuple[str, str]:
    session = boto3.Session(region_name=region)
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    account_id = identity["Account"]
    arn = identity["Arn"]
    print(f"AWS identity: {arn}")
    return account_id, region


def ensure_ecr_repo(region: str, repository_name: str) -> str:
    ecr = boto3.client("ecr", region_name=region)
    try:
        response = ecr.describe_repositories(repositoryNames=[repository_name])
        repo = response["repositories"][0]
        return repo["repositoryUri"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "RepositoryNotFoundException":
            raise
        created = ecr.create_repository(
            repositoryName=repository_name,
            imageScanningConfiguration={"scanOnPush": True},
        )
        return created["repository"]["repositoryUri"]


def docker_login_ecr(region: str) -> None:
    ecr = boto3.client("ecr", region_name=region)
    token_data = ecr.get_authorization_token()["authorizationData"][0]
    token = base64.b64decode(token_data["authorizationToken"]).decode("utf-8")
    username, password = token.split(":", 1)
    endpoint = token_data["proxyEndpoint"]
    print("+ docker login -u AWS --password-stdin", endpoint)
    subprocess.run(
        ["docker", "login", "-u", username, "--password-stdin", endpoint],
        input=password,
        text=True,
        check=True,
    )


def build_and_push_image(ecr_uri: str, image_tag: str) -> str:
    local_image = "medgemma-challenge:latest"
    remote_image = f"{ecr_uri}:{image_tag}"
    run(
        [
            "docker",
            "build",
            "-f",
            str(DEPLOY_ROOT / "Dockerfile"),
            "-t",
            local_image,
            str(ROOT),
        ]
    )
    run(["docker", "tag", local_image, remote_image])
    run(["docker", "push", remote_image])
    return remote_image


def ensure_apprunner_access_role(region: str, role_name: str) -> str:
    iam = boto3.client("iam", region_name=region)
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "build.apprunner.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    try:
        role = iam.get_role(RoleName=role_name)["Role"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "NoSuchEntity":
            raise
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Access role for App Runner to pull images from ECR",
        )["Role"]
        time.sleep(8)

    policy_arn = "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
    iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
    return role["Arn"]


def find_service_arn(apprunner, service_name: str) -> str | None:
    next_token = None
    while True:
        kwargs = {}
        if next_token:
            kwargs["NextToken"] = next_token
        page = apprunner.list_services(**kwargs)
        for svc in page.get("ServiceSummaryList", []):
            if svc.get("ServiceName") == service_name:
                return svc.get("ServiceArn")
        next_token = page.get("NextToken")
        if not next_token:
            break
    return None


def deploy_service(config: DeployConfig, image_identifier: str, access_role_arn: str) -> str:
    apprunner = boto3.client("apprunner", region_name=config.region)
    source_config = {
        "AuthenticationConfiguration": {"AccessRoleArn": access_role_arn},
        "AutoDeploymentsEnabled": False,
        "ImageRepository": {
            "ImageIdentifier": image_identifier,
            "ImageRepositoryType": "ECR",
            "ImageConfiguration": {
                "Port": "8010",
                "RuntimeEnvironmentVariables": {
                    "MODEL_BACKEND": config.model_backend,
                    "APP_NAME": "MedGemma Discharge Copilot",
                },
            },
        },
    }
    health = {
        "Protocol": "HTTP",
        "Path": "/health",
        "Interval": 10,
        "Timeout": 5,
        "HealthyThreshold": 1,
        "UnhealthyThreshold": 5,
    }

    existing_arn = find_service_arn(apprunner, config.service_name)
    if existing_arn:
        apprunner.update_service(
            ServiceArn=existing_arn,
            SourceConfiguration=source_config,
            HealthCheckConfiguration=health,
        )
        service_arn = existing_arn
        print(f"Updated App Runner service: {config.service_name}")
    else:
        created = apprunner.create_service(
            ServiceName=config.service_name,
            SourceConfiguration=source_config,
            HealthCheckConfiguration=health,
        )
        service_arn = created["Service"]["ServiceArn"]
        print(f"Created App Runner service: {config.service_name}")

    return wait_for_running(apprunner, service_arn)


def wait_for_running(apprunner, service_arn: str, timeout_sec: int = 1800) -> str:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        service = apprunner.describe_service(ServiceArn=service_arn)["Service"]
        status = service.get("Status", "UNKNOWN")
        print(f"Service status: {status}")
        if status == "RUNNING":
            url = service.get("ServiceUrl", "")
            if url and not url.startswith("http"):
                url = f"https://{url}"
            return url
        if status in {"CREATE_FAILED", "DELETE_FAILED"}:
            raise RuntimeError(f"App Runner service failed with status: {status}")
        time.sleep(20)
    raise TimeoutError("Timed out waiting for service to become RUNNING")


def write_outputs(url: str) -> None:
    out_path = DEPLOY_ROOT / "live_demo_url.txt"
    out_path.write_text(url + "\n", encoding="utf-8")
    print(f"Live URL saved to {out_path}")
    print(f"Live demo URL: {url}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy MedGemma challenge to AWS App Runner")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--app-name", default="medgemma-discharge-copilot")
    parser.add_argument("--image-tag", default=f"build-{int(time.time())}")
    parser.add_argument("--model-backend", default="mock", choices=["mock", "transformers", "openai_compatible"])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app_name = sanitize_name(args.app_name)
    repository = f"{app_name}-repo"
    service_name = f"{app_name}-svc"
    role_name = sanitize_name(f"{app_name}-apprunner-ecr-role", max_len=60)

    config = DeployConfig(
        app_name=app_name,
        region=args.region,
        repository=repository,
        service_name=service_name,
        image_tag=args.image_tag,
        model_backend=args.model_backend,
    )

    try:
        run(["docker", "info"])
        verify_aws_access(config.region)
        ecr_uri = ensure_ecr_repo(config.region, config.repository)
        docker_login_ecr(config.region)
        image_identifier = build_and_push_image(ecr_uri, config.image_tag)
        access_role_arn = ensure_apprunner_access_role(config.region, role_name)
        url = deploy_service(config, image_identifier, access_role_arn)
        write_outputs(url)
    except NoCredentialsError:
        print("AWS credentials not found.")
        print("Set AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and AWS_REGION, then rerun.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
