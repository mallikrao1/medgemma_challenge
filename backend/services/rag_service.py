"""
RAG Service for Terraform Documentation.
Uses lazy imports to avoid slow startup.
Gracefully handles missing Ollama or ChromaDB.
"""

import os
import asyncio
from typing import List, Dict, Optional
from pathlib import Path
from config import settings


class RAGService:
    def __init__(self):
        self.vector_db_path = settings.VECTOR_DB_PATH
        self.embedding_model = settings.OLLAMA_EMBEDDING_MODEL
        self.client = None
        self.collection = None
        self.initialized = False

    async def initialize(self):
        """Initialize ChromaDB. Lazy imports to avoid slow startup."""
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings
        except ImportError:
            print("  ChromaDB not available - RAG disabled")
            return

        try:
            print("  Initializing vector database...")
            self.client = chromadb.Client(ChromaSettings(
                is_persistent=True,
                persist_directory=self.vector_db_path,
            ))

            try:
                self.collection = self.client.get_collection(name="terraform_docs")
                count = self.collection.count()
                print(f"  Loaded existing collection with {count} documents")
            except Exception:
                self.collection = self.client.create_collection(name="terraform_docs")
                print("  Created new collection")
                try:
                    await self._ingest_docs()
                except Exception as e:
                    print(f"  Doc ingestion failed (non-critical): {e}")

            self.initialized = True

        except Exception as e:
            print(f"  RAG init failed (non-critical): {e}")
            self.initialized = False

    async def _ingest_docs(self):
        """Ingest basic Terraform docs."""
        try:
            import ollama as _ollama
        except ImportError:
            return

        docs = [
            {"id": "s3", "content": "AWS S3 with boto3 client('s3'): create_bucket, put_bucket_versioning, put_bucket_encryption.", "provider": "aws", "type": "s3"},
            {"id": "ec2", "content": "AWS EC2 with boto3 client('ec2'): describe_images, run_instances, terminate_instances.", "provider": "aws", "type": "ec2"},
            {"id": "rds", "content": "AWS RDS with boto3 client('rds'): create_db_instance, delete_db_instance, describe_db_instances.", "provider": "aws", "type": "rds"},
            {"id": "lambda", "content": "AWS Lambda with boto3 client('lambda'): create_function, update_function_code, delete_function.", "provider": "aws", "type": "lambda"},
            {"id": "vpc", "content": "AWS VPC with boto3 client('ec2'): create_vpc, create_subnet, create_internet_gateway.", "provider": "aws", "type": "vpc"},
            {"id": "dynamodb", "content": "AWS DynamoDB with boto3 client('dynamodb'): create_table, delete_table, describe_table.", "provider": "aws", "type": "dynamodb"},
            {"id": "iam", "content": "AWS IAM with boto3 client('iam'): create_role, create_user, create_policy.", "provider": "aws", "type": "iam"},
            {"id": "sns", "content": "AWS SNS with boto3 client('sns'): create_topic, publish, subscribe.", "provider": "aws", "type": "sns"},
            {"id": "sqs", "content": "AWS SQS with boto3 client('sqs'): create_queue, send_message, delete_queue.", "provider": "aws", "type": "sqs"},
            {"id": "ecs", "content": "AWS ECS with boto3 client('ecs'): create_cluster, register_task_definition, create_service.", "provider": "aws", "type": "ecs"},
            {"id": "eks", "content": "AWS EKS with boto3 client('eks'): create_cluster, create_nodegroup, describe_cluster.", "provider": "aws", "type": "eks"},
            {"id": "route53", "content": "AWS Route53 with boto3 client('route53'): create_hosted_zone, change_resource_record_sets.", "provider": "aws", "type": "route53"},
            {"id": "cloudfront", "content": "AWS CloudFront with boto3 client('cloudfront'): create_distribution, get_distribution, delete_distribution.", "provider": "aws", "type": "cloudfront"},
            {"id": "elasticache", "content": "AWS ElastiCache with boto3 client('elasticache'): create_cache_cluster, create_replication_group.", "provider": "aws", "type": "elasticache"},
            {"id": "kinesis", "content": "AWS Kinesis with boto3 client('kinesis'): create_stream, put_record, delete_stream.", "provider": "aws", "type": "kinesis"},
            {"id": "secretsmanager", "content": "AWS Secrets Manager with boto3 client('secretsmanager'): create_secret, put_secret_value.", "provider": "aws", "type": "secretsmanager"},
            {"id": "ssm", "content": "AWS SSM with boto3 client('ssm'): put_parameter, get_parameter, delete_parameter.", "provider": "aws", "type": "ssm"},
            {"id": "ecr", "content": "AWS ECR with boto3 client('ecr'): create_repository, describe_repositories, delete_repository.", "provider": "aws", "type": "ecr"},
            {"id": "stepfunctions", "content": "AWS Step Functions with boto3 client('stepfunctions'): create_state_machine, start_execution.", "provider": "aws", "type": "stepfunctions"},
            {"id": "apigateway", "content": "AWS API Gateway with boto3 client('apigateway'): create_rest_api, put_method, create_deployment.", "provider": "aws", "type": "apigateway"},
            {"id": "codepipeline", "content": "AWS CodePipeline with boto3 client('codepipeline'): create_pipeline, start_pipeline_execution.", "provider": "aws", "type": "codepipeline"},
            {"id": "codebuild", "content": "AWS CodeBuild with boto3 client('codebuild'): create_project, start_build.", "provider": "aws", "type": "codebuild"},
            {"id": "glue", "content": "AWS Glue with boto3 client('glue'): create_database, create_crawler, start_job_run.", "provider": "aws", "type": "glue"},
            {"id": "athena", "content": "AWS Athena with boto3 client('athena'): start_query_execution, get_query_execution.", "provider": "aws", "type": "athena"},
            {"id": "redshift", "content": "AWS Redshift with boto3 client('redshift'): create_cluster, describe_clusters, delete_cluster.", "provider": "aws", "type": "redshift"},
            {"id": "emr", "content": "AWS EMR with boto3 client('emr'): run_job_flow, describe_cluster, terminate_job_flows.", "provider": "aws", "type": "emr"},
            {"id": "sagemaker", "content": "AWS SageMaker with boto3 client('sagemaker'): create_notebook_instance, create_training_job.", "provider": "aws", "type": "sagemaker"},
            {"id": "security_group", "content": "AWS Security Groups with boto3 client('ec2'): create_security_group, authorize_security_group_ingress.", "provider": "aws", "type": "security_group"},
            {"id": "ebs", "content": "AWS EBS with boto3 client('ec2'): create_volume, attach_volume, delete_volume.", "provider": "aws", "type": "ebs"},
            {"id": "efs", "content": "AWS EFS with boto3 client('efs'): create_file_system, create_mount_target.", "provider": "aws", "type": "efs"},
            {"id": "acm", "content": "AWS ACM with boto3 client('acm'): request_certificate, describe_certificate.", "provider": "aws", "type": "acm"},
            {"id": "kms", "content": "AWS KMS with boto3 client('kms'): create_key, create_alias, schedule_key_deletion.", "provider": "aws", "type": "kms"},
            {"id": "waf", "content": "AWS WAFv2 with boto3 client('wafv2'): create_web_acl, associate_web_acl.", "provider": "aws", "type": "waf"},
            {"id": "cloudwatch", "content": "AWS CloudWatch with boto3 client('cloudwatch'): put_metric_alarm, describe_alarms.", "provider": "aws", "type": "cloudwatch"},
            {"id": "elb", "content": "AWS ELBv2 with boto3 client('elbv2'): create_load_balancer, create_target_group, create_listener.", "provider": "aws", "type": "elb"},
        ]

        chunks, embeddings, metadatas, ids = [], [], [], []
        for doc in docs:
            try:
                resp = _ollama.embeddings(model=self.embedding_model, prompt=doc["content"])
                chunks.append(doc["content"])
                embeddings.append(resp["embedding"])
                metadatas.append({"provider": doc["provider"], "resource_type": doc["type"]})
                ids.append(f"doc_{doc['id']}")
            except Exception:
                pass

        if chunks and self.collection:
            self.collection.add(embeddings=embeddings, documents=chunks, metadatas=metadatas, ids=ids)
            print(f"  Ingested {len(chunks)} docs")

    async def retrieve(self, query: str, n_results: int = 5, provider: Optional[str] = None) -> List[Dict]:
        """Retrieve relevant documents."""
        if not self.initialized or not self.collection:
            return []
        try:
            import ollama as _ollama
            resp = _ollama.embeddings(model=self.embedding_model, prompt=query)
            where = {"provider": provider} if provider else None
            results = self.collection.query(query_embeddings=[resp["embedding"]], n_results=n_results, where=where)
            out = []
            if results and results["documents"] and results["documents"][0]:
                for i in range(len(results["documents"][0])):
                    out.append({"content": results["documents"][0][i], "metadata": results["metadatas"][0][i]})
            return out
        except Exception:
            return []

    def get_stats(self) -> Dict:
        if self.collection and self.initialized:
            return {"total_documents": self.collection.count(), "status": "active"}
        return {"status": "not initialized"}
