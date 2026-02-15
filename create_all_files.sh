cd ~/Library/Mobile\ Documents/com~apple~CloudDocs/ai-infra-platform

cat > create_all_files.sh << 'CREATEEOF'
#!/bin/bash

echo "🚀 Creating all required files for AI Infrastructure Platform..."
echo "================================================================"

# ==================== 1. NLU Service ====================
echo "📄 Creating nlu_service.py..."
cat > backend/services/nlu_service.py << 'NLUEOF'
"""
Advanced NLU Service - Extracts intent, entities, and parameters from natural language
"""

import re
import ollama
import json
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

@dataclass
class ParsedIntent:
    action: str  # create, update, delete, list, describe
    resource_type: str  # s3, ec2, rds, lambda, vpc, etc.
    resource_name: Optional[str] = None
    region: Optional[str] = None
    parameters: Dict[str, Any] = None
    confidence: float = 0.0
    
    def __post_init__(self):
        if self.parameters is None:
            self.parameters = {}

class NLUService:
    def __init__(self):
        self.model = "llama3.1:8b"
        
        # Action keywords
        self.action_patterns = {
            'create': ['create', 'make', 'build', 'provision', 'deploy', 'launch', 'setup', 'add'],
            'delete': ['delete', 'remove', 'destroy', 'terminate', 'kill', 'drop'],
            'update': ['update', 'modify', 'change', 'edit', 'alter', 'resize'],
            'list': ['list', 'show', 'display', 'get all', 'find all'],
            'describe': ['describe', 'show', 'get', 'details', 'info', 'status']
        }
        
        # Resource type patterns
        self.resource_patterns = {
            's3': ['s3', 'bucket', 'storage bucket', 'object storage'],
            'ec2': ['ec2', 'instance', 'virtual machine', 'vm', 'server'],
            'rds': ['rds', 'database', 'mysql', 'postgres', 'db instance'],
            'lambda': ['lambda', 'function', 'serverless function'],
            'vpc': ['vpc', 'network', 'virtual network'],
            'elb': ['load balancer', 'elb', 'alb', 'nlb'],
            'cloudwatch': ['alarm', 'cloudwatch', 'monitoring', 'alert']
        }
        
    async def parse_request(self, natural_language: str, user_region: Optional[str] = None) -> ParsedIntent:
        """Parse natural language into structured intent"""
        
        print(f"🧠 Parsing request: {natural_language}")
        
        # Use LLM to extract structured information
        prompt = f"""Extract information from this infrastructure request. Respond ONLY with valid JSON.

Request: "{natural_language}"

Extract:
1. action: create, update, delete, list, or describe
2. resource_type: s3, ec2, rds, lambda, vpc, etc.
3. resource_name: exact name if mentioned, otherwise null
4. region: AWS region if mentioned (us-east-1, us-west-2, etc.), otherwise null
5. parameters: dict of any other details (instance_type, storage_size, encryption, etc.)

Example output:
{{
  "action": "create",
  "resource_type": "s3",
  "resource_name": "my-data-bucket",
  "region": "us-west-2",
  "parameters": {{
    "encryption": true,
    "versioning": true
  }}
}}

JSON output:"""

        try:
            response = ollama.generate(model=self.model, prompt=prompt)
            raw_response = response["response"].strip()
            
            # Clean up response
            if "```json" in raw_response:
                raw_response = raw_response.split("```json")[1].split("```")[0].strip()
            elif "```" in raw_response:
                raw_response = raw_response.split("```")[1].split("```")[0].strip()
            
            parsed = json.loads(raw_response)
            
            # Override region with user's choice if provided
            if user_region:
                parsed["region"] = user_region
                
            intent = ParsedIntent(
                action=parsed.get("action", "create").lower(),
                resource_type=parsed.get("resource_type", "unknown").lower(),
                resource_name=parsed.get("resource_name"),
                region=parsed.get("region") or user_region or "us-east-1",
                parameters=parsed.get("parameters", {}),
                confidence=0.9
            )
            
            print(f"✅ Parsed: action={intent.action}, type={intent.resource_type}, name={intent.resource_name}, region={intent.region}")
            return intent
            
        except Exception as e:
            print(f"⚠️  LLM parsing failed: {e}, using fallback")
            return self._fallback_parse(natural_language, user_region)
    
    def _fallback_parse(self, text: str, user_region: Optional[str]) -> ParsedIntent:
        """Fallback parser using regex patterns"""
        text_lower = text.lower()
        
        # Detect action
        action = "create"
        for act, keywords in self.action_patterns.items():
            if any(kw in text_lower for kw in keywords):
                action = act
                break
        
        # Detect resource type
        resource_type = "unknown"
        for res, keywords in self.resource_patterns.items():
            if any(kw in text_lower for kw in keywords):
                resource_type = res
                break
        
        # Extract resource name
        resource_name = None
        
        # Pattern 1: "bucket named my-bucket" or "bucket called my-bucket"
        match = re.search(r'(?:named|called)\s+["\']?([a-z0-9-]+)["\']?', text_lower)
        if match:
            resource_name = match.group(1)
        
        # Pattern 2: Quoted names
        if not resource_name:
            match = re.search(r'["\']([a-z0-9-]+)["\']', text)
            if match:
                resource_name = match.group(1)
        
        # Detect region
        region = user_region
        region_patterns = [
            r'(us-east-1|us-east-2|us-west-1|us-west-2)',
            r'(eu-west-1|eu-central-1|eu-north-1)',
            r'(ap-south-1|ap-southeast-1|ap-northeast-1)'
        ]
        for pattern in region_patterns:
            match = re.search(pattern, text_lower)
            if match:
                region = match.group(1)
                break
        
        # Extract parameters
        parameters = {}
        if 'encrypt' in text_lower:
            parameters['encryption'] = True
        if 'version' in text_lower:
            parameters['versioning'] = True
        if 't3.micro' in text_lower or 't3.small' in text_lower:
            match = re.search(r't[23]\.(micro|small|medium|large)', text_lower)
            if match:
                parameters['instance_type'] = match.group(0)
        
        return ParsedIntent(
            action=action,
            resource_type=resource_type,
            resource_name=resource_name,
            region=region or "us-east-1",
            parameters=parameters,
            confidence=0.7
        )
NLUEOF

echo "✅ nlu_service.py created"

# ==================== 2. AWS Executor ====================
echo "📄 Creating aws_executor.py..."
cat > backend/services/aws_executor.py << 'AWSEOF'
"""
Enhanced AWS Executor - Full CRUD operations with exact naming
"""

import boto3
import uuid
from typing import Dict, Any, Optional, List
from botocore.exceptions import ClientError

class AWSExecutor:
    def __init__(self):
        self.ec2 = None
        self.s3 = None
        self.rds = None
        self.region = None
        self.initialized = False
        
    def initialize(self, access_key: str, secret_key: str, region: str):
        """Initialize AWS clients"""
        try:
            self.ec2 = boto3.client('ec2', aws_access_key_id=access_key, 
                                   aws_secret_access_key=secret_key, region_name=region)
            self.s3 = boto3.client('s3', aws_access_key_id=access_key,
                                  aws_secret_access_key=secret_key, region_name=region)
            self.rds = boto3.client('rds', aws_access_key_id=access_key,
                                   aws_secret_access_key=secret_key, region_name=region)
            self.region = region
            self.initialized = True
            print(f"✅ AWS initialized: {region}")
            return True
        except Exception as e:
            print(f"❌ AWS init failed: {e}")
            return False
    
    # ==================== S3 OPERATIONS ====================
    
    async def create_s3_bucket(self, name: str, params: Dict = None) -> Dict:
        """Create S3 bucket with exact name"""
        try:
            print(f"🪣 Creating S3 bucket: {name} in {self.region}")
            
            # Create bucket
            if self.region == 'us-east-1':
                self.s3.create_bucket(Bucket=name)
            else:
                self.s3.create_bucket(
                    Bucket=name,
                    CreateBucketConfiguration={'LocationConstraint': self.region}
                )
            
            # Apply configurations
            if params.get('versioning', True):
                self.s3.put_bucket_versioning(
                    Bucket=name,
                    VersioningConfiguration={'Status': 'Enabled'}
                )
            
            if params.get('encryption', True):
                self.s3.put_bucket_encryption(
                    Bucket=name,
                    ServerSideEncryptionConfiguration={
                        'Rules': [{'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]
                    }
                )
            
            # Tags
            tags = params.get('tags', {})
            if tags:
                self.s3.put_bucket_tagging(
                    Bucket=name,
                    Tagging={'TagSet': [{'Key': k, 'Value': v} for k, v in tags.items()]}
                )
            
            return {
                "success": True,
                "action": "created",
                "resource_type": "s3_bucket",
                "resource_name": name,
                "region": self.region,
                "arn": f"arn:aws:s3:::{name}",
                "details": {
                    "versioning": params.get('versioning', True),
                    "encryption": "AES256"
                }
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}
    
    async def delete_s3_bucket(self, name: str) -> Dict:
        """Delete S3 bucket"""
        try:
            print(f"🗑️  Deleting S3 bucket: {name}")
            
            # Delete all objects first
            try:
                objects = self.s3.list_objects_v2(Bucket=name)
                if 'Contents' in objects:
                    self.s3.delete_objects(
                        Bucket=name,
                        Delete={'Objects': [{'Key': obj['Key']} for obj in objects['Contents']]}
                    )
            except:
                pass
            
            # Delete bucket
            self.s3.delete_bucket(Bucket=name)
            
            return {
                "success": True,
                "action": "deleted",
                "resource_type": "s3_bucket",
                "resource_name": name
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}
    
    async def update_s3_bucket(self, name: str, params: Dict) -> Dict:
        """Update S3 bucket configuration"""
        try:
            print(f"📝 Updating S3 bucket: {name}")
            
            updates = []
            
            if 'versioning' in params:
                status = 'Enabled' if params['versioning'] else 'Suspended'
                self.s3.put_bucket_versioning(
                    Bucket=name,
                    VersioningConfiguration={'Status': status}
                )
                updates.append(f"versioning={status}")
            
            if 'tags' in params:
                self.s3.put_bucket_tagging(
                    Bucket=name,
                    Tagging={'TagSet': [{'Key': k, 'Value': v} for k, v in params['tags'].items()]}
                )
                updates.append("tags updated")
            
            return {
                "success": True,
                "action": "updated",
                "resource_type": "s3_bucket",
                "resource_name": name,
                "changes": updates
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}
    
    async def list_s3_buckets(self) -> Dict:
        """List all S3 buckets"""
        try:
            response = self.s3.list_buckets()
            buckets = [b['Name'] for b in response.get('Buckets', [])]
            return {
                "success": True,
                "action": "listed",
                "resource_type": "s3_bucket",
                "count": len(buckets),
                "resources": buckets
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}
    
    # ==================== EC2 OPERATIONS ====================
    
    async def create_ec2_instance(self, name: str, params: Dict) -> Dict:
        """Create EC2 instance"""
        try:
            instance_type = params.get('instance_type', 't3.micro')
            ami_id = params.get('ami_id')
            
            if not ami_id:
                # Get latest Amazon Linux 2
                images = self.ec2.describe_images(
                    Owners=['amazon'],
                    Filters=[
                        {'Name': 'name', 'Values': ['amzn2-ami-hvm-*-x86_64-gp2']},
                        {'Name': 'state', 'Values': ['available']}
                    ]
                )
                ami_id = sorted(images['Images'], key=lambda x: x['CreationDate'], reverse=True)[0]['ImageId']
            
            print(f"🖥️  Creating EC2: {name} ({instance_type}) in {self.region}")
            
            response = self.ec2.run_instances(
                ImageId=ami_id,
                InstanceType=instance_type,
                MinCount=1,
                MaxCount=1,
                TagSpecifications=[{
                    'ResourceType': 'instance',
                    'Tags': [{'Key': 'Name', 'Value': name}] + 
                           [{'Key': k, 'Value': v} for k, v in params.get('tags', {}).items()]
                }],
                Monitoring={'Enabled': params.get('monitoring', True)}
            )
            
            instance = response['Instances'][0]
            
            return {
                "success": True,
                "action": "created",
                "resource_type": "ec2_instance",
                "resource_name": name,
                "instance_id": instance['InstanceId'],
                "instance_type": instance_type,
                "region": self.region,
                "state": instance['State']['Name']
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}
    
    async def delete_ec2_instance(self, name: str = None, instance_id: str = None) -> Dict:
        """Delete EC2 instance by name or ID"""
        try:
            if not instance_id and name:
                # Find by name tag
                response = self.ec2.describe_instances(
                    Filters=[{'Name': 'tag:Name', 'Values': [name]}]
                )
                instances = []
                for r in response['Reservations']:
                    instances.extend(r['Instances'])
                if instances:
                    instance_id = instances[0]['InstanceId']
            
            if not instance_id:
                return {"success": False, "error": "Instance not found"}
            
            print(f"🗑️  Terminating EC2: {instance_id}")
            
            self.ec2.terminate_instances(InstanceIds=[instance_id])
            
            return {
                "success": True,
                "action": "deleted",
                "resource_type": "ec2_instance",
                "instance_id": instance_id
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}
    
    async def list_ec2_instances(self) -> Dict:
        """List all EC2 instances"""
        try:
            response = self.ec2.describe_instances()
            instances = []
            for r in response['Reservations']:
                for i in r['Instances']:
                    name = next((t['Value'] for t in i.get('Tags', []) if t['Key'] == 'Name'), 'N/A')
                    instances.append({
                        'name': name,
                        'instance_id': i['InstanceId'],
                        'type': i['InstanceType'],
                        'state': i['State']['Name']
                    })
            
            return {
                "success": True,
                "action": "listed",
                "resource_type": "ec2_instance",
                "count": len(instances),
                "resources": instances
            }
        except ClientError as e:
            return {"success": False, "error": str(e)}
    
    # ==================== GENERIC EXECUTOR ====================
    
    async def execute(self, action: str, resource_type: str, name: str, params: Dict) -> Dict:
        """Generic executor that routes to specific methods"""
        
        try:
            if resource_type == 's3':
                if action == 'create':
                    return await self.create_s3_bucket(name, params)
                elif action == 'delete':
                    return await self.delete_s3_bucket(name)
                elif action == 'update':
                    return await self.update_s3_bucket(name, params)
                elif action == 'list':
                    return await self.list_s3_buckets()
                    
            elif resource_type == 'ec2':
                if action == 'create':
                    return await self.create_ec2_instance(name, params)
                elif action == 'delete':
                    return await self.delete_ec2_instance(name=name)
                elif action == 'list':
                    return await self.list_ec2_instances()
            
            return {"success": False, "error": f"Unsupported: {action} {resource_type}"}
            
        except Exception as e:
            return {"success": False, "error": str(e)}
AWSEOF

echo "✅ aws_executor.py created"

# ==================== 3. Orchestrator ====================
echo "📄 Creating orchestrator.py..."
cat > backend/services/orchestrator.py << 'ORCHEOF'
"""
Intelligent Orchestrator with NLU
"""

from typing import Dict, Any
from services.nlu_service import NLUService, ParsedIntent
from services.aws_executor import AWSExecutor

class OrchestrationService:
    def __init__(self, rag_service):
        self.rag_service = rag_service
        self.nlu = NLUService()
        self.aws = AWSExecutor()
        
    async def process_request(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process user request with full NLU"""
        
        try:
            # 1. Parse natural language
            print("\n" + "="*60)
            print("🚀 STARTING REQUEST PROCESSING")
            print("="*60)
            
            intent = await self.nlu.parse_request(
                request_data["natural_language_request"],
                request_data.get("aws_region")
            )
            
            print(f"\n📋 PARSED INTENT:")
            print(f"   Action: {intent.action}")
            print(f"   Resource: {intent.resource_type}")
            print(f"   Name: {intent.resource_name}")
            print(f"   Region: {intent.region}")
            print(f"   Params: {intent.parameters}")
            
            # 2. Initialize AWS
            if not self.aws.initialized:
                success = self.aws.initialize(
                    request_data["aws_access_key"],
                    request_data["aws_secret_key"],
                    intent.region
                )
                if not success:
                    return {"success": False, "error": "AWS initialization failed"}
            
            # 3. Generate resource name if not provided
            if not intent.resource_name and intent.action == 'create':
                import uuid
                intent.resource_name = f"{intent.resource_type}-{uuid.uuid4().hex[:8]}"
                print(f"   Generated name: {intent.resource_name}")
            
            # 4. Add default tags
            if 'tags' not in intent.parameters:
                intent.parameters['tags'] = {}
            intent.parameters['tags'].update({
                'ManagedBy': 'AI-Platform',
                'CreatedBy': request_data.get('requester_id', 'unknown'),
                'Environment': request_data.get('environment', 'dev')
            })
            
            # 5. Execute action
            print(f"\n⚡ EXECUTING: {intent.action.upper()} {intent.resource_type}")
            
            result = await self.aws.execute(
                action=intent.action,
                resource_type=intent.resource_type,
                name=intent.resource_name,
                params=intent.parameters
            )
            
            print(f"\n✅ RESULT: {result.get('action', 'completed').upper()}")
            print("="*60 + "\n")
            
            return {
                "request_id": request_data["request_id"],
                "status": "completed" if result.get("success") else "failed",
                "intent": {
                    "action": intent.action,
                    "resource_type": intent.resource_type,
                    "resource_name": intent.resource_name,
                    "region": intent.region
                },
                "execution_result": result
            }
            
        except Exception as e:
            print(f"\n❌ ERROR: {str(e)}\n")
            return {
                "request_id": request_data["request_id"],
                "status": "failed",
                "error": str(e)
            }
ORCHEOF

echo "✅ orchestrator.py created"

# ==================== 4. Enhanced CSS ====================
echo "📄 Creating main.css..."
cat > frontend/src/styles/main.css << 'CSSEOF'
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Outfit:wght@300;400;500;600;700;800&display=swap');

:root {
  --color-bg: #0a0e1a;
  --color-surface: #131824;
  --color-surface-elevated: #1a2030;
  --color-border: #2a3548;
  --color-primary: #00d9ff;
  --color-secondary: #7c3aed;
  --color-success: #10b981;
  --color-error: #ef4444;
  --color-text: #e5e7eb;
  --color-text-secondary: #9ca3af;
  --color-text-muted: #6b7280;
  --font-display: 'Outfit', sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: var(--font-display);
  background: var(--color-bg);
  color: var(--color-text);
  line-height: 1.6;
  min-height: 100vh;
}

.app {
  max-width: 1600px;
  margin: 0 auto;
  padding: 2rem;
}

.header {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 20px;
  padding: 1.5rem 2rem;
  margin-bottom: 2rem;
}

.header-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1.5rem;
}

.logo {
  display: flex;
  align-items: center;
  gap: 1rem;
}

.logo-icon {
  width: 40px;
  height: 40px;
  color: var(--color-primary);
}

.logo-text {
  font-size: 1.75rem;
  font-weight: 800;
  background: linear-gradient(135deg, var(--color-primary), var(--color-secondary));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}

.logo-subtitle {
  font-size: 0.75rem;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 2px;
}

.status-indicator {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.75rem 1.25rem;
  background: rgba(0, 217, 255, 0.1);
  border: 1px solid var(--color-primary);
  border-radius: 12px;
}

.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--color-error);
}

.status-dot.active {
  background: var(--color-success);
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.7; transform: scale(1.1); }
}

.status-text {
  font-size: 0.9rem;
  font-weight: 600;
}

.agent-workspace {
  display: flex;
  flex-direction: column;
  gap: 2rem;
}

.workspace-header {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 20px;
  padding: 2rem;
}

.header-content-agent {
  display: flex;
  align-items: center;
  gap: 1.5rem;
}

.header-icon {
  width: 48px;
  height: 48px;
  color: var(--color-primary);
}

.workspace-title {
  font-size: 2.5rem;
  font-weight: 800;
  margin-bottom: 0.5rem;
}

.workspace-subtitle {
  color: var(--color-text-secondary);
  font-size: 1.1rem;
}

.workspace-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(450px, 1fr));
  gap: 1.5rem;
}

.workspace-panel {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 20px;
  padding: 2rem;
  min-height: 600px;
  display: flex;
  flex-direction: column;
}

.panel-header {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 1.5rem;
  padding-bottom: 1rem;
  border-bottom: 2px solid var(--color-border);
}

.panel-header h3 {
  flex: 1;
  font-size: 1.25rem;
  font-weight: 700;
}

.agent-form {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
  flex: 1;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.form-label {
  font-weight: 600;
  font-size: 0.95rem;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.form-textarea,
.form-select,
.form-input {
  background: var(--color-surface-elevated);
  border: 2px solid var(--color-border);
  border-radius: 12px;
  padding: 1rem 1.25rem;
  color: var(--color-text);
  font-size: 1rem;
  font-family: var(--font-display);
  transition: all 0.2s;
}

.form-textarea {
  font-family: var(--font-mono);
  resize: vertical;
}

.form-textarea:focus,
.form-select:focus,
.form-input:focus {
  outline: none;
  border-color: var(--color-primary);
}

.form-row {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 1.5rem;
}

.credentials-section {
  background: rgba(124, 58, 237, 0.1);
  border: 1px solid var(--color-secondary);
  border-radius: 12px;
  padding: 1.5rem;
}

.credentials-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 1rem;
}

.examples {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-top: 0.75rem;
}

.examples-label {
  font-size: 0.85rem;
  color: var(--color-text-muted);
  font-weight: 600;
  width: 100%;
  margin-bottom: 0.25rem;
}

.example-chip {
  background: var(--color-surface-elevated);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 0.5rem 1rem;
  font-size: 0.85rem;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.2s;
  font-family: var(--font-mono);
}

.example-chip:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
  transform: translateY(-2px);
}

.submit-button {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  padding: 1.25rem 2rem;
  background: linear-gradient(135deg, var(--color-primary), var(--color-secondary));
  border: none;
  border-radius: 12px;
  color: white;
  font-size: 1.1rem;
  font-weight: 600;
  cursor: pointer;
  font-family: var(--font-display);
  margin-top: auto;
  transition: all 0.3s;
}

.submit-button:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 8px 20px rgba(0, 217, 255, 0.3);
}

.submit-button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.button-icon {
  width: 20px;
  height: 20px;
}

.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.code-output {
  flex: 1;
  display: flex;
  flex-direction: column;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex: 1;
  gap: 1rem;
}

.empty-icon {
  color: var(--color-text-secondary);
  opacity: 0.5;
}

.code-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.result-status {
  margin-bottom: 1.5rem;
}

.status-success, .status-error {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 1rem 1.5rem;
  border-radius: 12px;
  font-weight: 600;
  font-size: 1.1rem;
}

.status-success {
  background: rgba(16, 185, 129, 0.1);
  border: 2px solid var(--color-success);
  color: var(--color-success);
}

.status-error {
  background: rgba(239, 68, 68, 0.1);
  border: 2px solid var(--color-error);
  color: var(--color-error);
}

.intent-display {
  background: var(--color-surface-elevated);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 1.5rem;
  margin-bottom: 1.5rem;
}

.intent-title {
  font-size: 0.9rem;
  font-weight: 700;
  color: var(--color-primary);
  margin-bottom: 1rem;
  text-transform: uppercase;
  letter-spacing: 1px;
}

.intent-items {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0.75rem;
}

.intent-item {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.intent-label {
  font-size: 0.75rem;
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  font-weight: 600;
}

.intent-value {
  font-size: 1rem;
  color: var(--color-text);
  font-family: var(--font-mono);
  font-weight: 600;
}

.result-details {
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 1.5rem;
}

.details-title {
  font-size: 0.9rem;
  font-weight: 700;
  color: var(--color-secondary);
  margin-bottom: 1rem;
  text-transform: uppercase;
  letter-spacing: 1px;
}

.code-block {
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 0.85rem;
  line-height: 1.8;
  color: var(--color-text);
  background: var(--color-bg);
  padding: 1.5rem;
  border-radius: 12px;
  max-height: 400px;
}

@media (max-width: 1024px) {
  .workspace-grid {
    grid-template-columns: 1fr;
  }
}
CSSEOF

echo "✅ main.css created"

echo ""
echo "================================================================"
echo "✅ All files created successfully!"
echo "================================================================"
echo ""
echo "Files created:"
echo "  1. backend/services/nlu_service.py"
echo "  2. backend/services/aws_executor.py"
echo "  3. backend/services/orchestrator.py"
echo "  4. frontend/src/styles/main.css"
echo ""
echo "Next steps:"
echo "  1. Restart platform: ./start.sh"
echo "  2. Open http://localhost:5173"
echo "  3. Enter AWS credentials"
echo "  4. Try: 'Create an S3 bucket named my-test-bucket'"
echo ""
CREATEEOF

chmod +x create_all_files.sh
./create_all_files.sh
