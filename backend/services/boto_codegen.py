import json
from typing import Optional, Dict, Any
from config import settings
from services.model_router import ModelRouter

class BotoCodeGenService:
    def __init__(self):
        self.model = settings.CODEGEN_PRIMARY_MODEL or settings.OLLAMA_CODEGEN_MODEL
        self.model_router = ModelRouter()

    async def generate_code(self, prompt: str, context: Dict[str, Any] = None) -> Optional[str]:
        """
        Generate dynamic Boto3 Python code based on the user's natural language request.
        """
        print(f"  Generating Boto3 code for: {prompt[:50]}...")
        
        system_prompt = """System Rules:
1. Use ONLY the 'boto3' library.
2. Assume a boto3 session is already available as 'session'.
3. Use 'session.client()' or 'session.resource()' to interact with services.
4. Output ONLY the Python code. No markdown fences, no explanations.
5. Include robust error handling with try-except blocks.
6. Print the results of the operations so they can be captured.
7. CRITICAL: NEVER hardcode AMI IDs. Always use `ec2.describe_images` with filters (e.g., Name='name', Values=['amzn2-ami-hvm-*-x86_64-gp2']) to find the latest AMI dynamically.
8. CRITICAL: NEVER use index access like list[0] without verifying the list is not empty.
9. TO REPORT SUCCESS: The script must build a dictionary called `result_summary` and print it as a JSON string at the very end.
   - Example: result_summary = {"success": True, "instance_id": "i-123...", "message": "..."}
   - print(json.dumps(result_summary))
10. ALWAYS use lowercase keys in result_summary: `success`, `error`, `details`, `message`, `resource_type`, `action`.
11. On any exception, store the real AWS error message in `result_summary["error"]` (do not hide it behind generic text).
12. NEVER set VpcId to placeholders like "default", "null", or "none". If VPC is needed, resolve it dynamically with `describe_vpcs(Filters=[{"Name":"isDefault","Values":["true"]}])`.
13. For EC2 create requests with app installation intent, include `UserData` from context intent parameters when provided.
14. This agent must handle ANY AWS service requested by the user. Use the required boto3 client for that service and execute exact intent.
15. Always create clients from `session.client(...)` and resources from `session.resource(...)`, not `boto3.client(...)` or `boto3.resource(...)`.
16. If a region is required, use `region` variable from runtime context or `session.region_name`.
17. NEVER use interactive prompts like `input()`, `raw_input()`, or any stdin read. All values must come from context or safe defaults.

Example Structure:
import boto3, json
ec2 = session.client('ec2')
result_summary = {"success": False, "details": {}}
try:
    # 1. Find AMI
    images = ec2.describe_images(Filters=[...])['Images']
    if not images: raise Exception("No AMI found")
    ami_id = images[0]['ImageId']
    # 2. Run Instance
    res = ec2.run_instances(ImageId=ami_id, ...)
    result_summary["success"] = True
    result_summary["instance_id"] = res['Instances'][0]['InstanceId']
except Exception as e:
    result_summary["error"] = str(e)
print(json.dumps(result_summary))
"""

        full_prompt = f"User Request: {prompt}\nContext: {json.dumps(context or {})}\n\nPython Code:"
        
        try:
            response = self.model_router.generate(
                task="codegen",
                system=system_prompt,
                prompt=full_prompt,
                options={"temperature": 0},
            )
            code = response["response"].strip()
            
            # Clean up potential markdown fences
            if "```python" in code:
                code = code.split("```python")[1].split("```")[0].strip()
            elif "```" in code:
                code = code.split("```")[1].split("```")[0].strip()
                
            return code
        except Exception as e:
            print(f"  Boto3 code generation failed: {e}")
            return None

    async def repair_code(
        self,
        prompt: str,
        context: Dict[str, Any],
        failed_code: str,
        error: Optional[str] = None,
        output: Optional[str] = None,
    ) -> Optional[str]:
        """Regenerate fixed boto3 code using failure feedback."""
        repair_system_prompt = """You are repairing a failed boto3 automation script.
Rules:
1. Return ONLY executable Python code (no markdown).
2. Keep the same business intent as the original user request.
3. Fix the exact runtime error provided.
4. Preserve robust try/except handling and always print `json.dumps(result_summary)` at the end.
5. Use lowercase keys in `result_summary`: success, error, details, message, action, resource_type.
6. Never use placeholder identifiers such as VpcId='default', None-like strings, or fake ARNs/IDs.
7. If any list can be empty, guard before indexing.
8. If the request references services not covered by static handlers, still implement it dynamically with boto3.
9. Use `session.client(...)`/`session.resource(...)` and preserve region from runtime (`region` or `session.region_name`).
10. Never use `input()` or any interactive stdin call.
"""
        repair_prompt = (
            f"User Request: {prompt}\n"
            f"Context: {json.dumps(context or {})}\n"
            f"Failed Code:\n{failed_code}\n\n"
            f"Runtime Error: {error or 'unknown'}\n"
            f"Runtime Output: {output or ''}\n\n"
            "Return corrected Python code:"
        )
        try:
            response = self.model_router.generate(
                task="codegen_repair",
                system=repair_system_prompt,
                prompt=repair_prompt,
                options={"temperature": 0},
            )
            code = response["response"].strip()
            if "```python" in code:
                code = code.split("```python")[1].split("```")[0].strip()
            elif "```" in code:
                code = code.split("```")[1].split("```")[0].strip()
            return code
        except Exception as e:
            print(f"  Boto3 repair generation failed: {e}")
            return None
