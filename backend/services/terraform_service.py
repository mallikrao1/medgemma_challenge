import asyncio
import subprocess
import json
import os
from typing import Dict, Any, Optional
from pathlib import Path
from config import settings
from services.model_router import ModelRouter

class TerraformService:
    def __init__(self):
        # Source root containing all modules
        self.source_root = Path(__file__).resolve().parent.parent.parent / "terraform-modules"
        # Isolated execution root
        self.iso_root = Path("/tmp/terraform_storage")
        # Current active execution directory
        self.tf_dir = self.iso_root / "aws"
        self.model_router = ModelRouter()
        self._ensure_init()

    def _ensure_init(self):
        """Ensure Terraform is initialized with complete module mirroring."""
        try:
            import shutil
            # 1. Prepare isolated storage
            if not self.iso_root.exists():
                self.iso_root.mkdir(parents=True, exist_ok=True)
            
            # 2. Mirror ENTIRE terraform-modules tree to avoid broken relative paths
            if self.source_root.exists():
                # We copy everything BUT the .terraform directories to start fresh
                for item in self.source_root.iterdir():
                    dest_path = self.iso_root / item.name
                    if item.is_dir():
                        if item.name == ".terraform": continue
                        if dest_path.exists(): shutil.rmtree(dest_path)
                        shutil.copytree(item, dest_path, ignore=shutil.ignore_patterns('.terraform*', 'terraform.tfstate*'))
                    else:
                        shutil.copy2(item, dest_path)
            
            # 3. Initialize in the isolated aws directory
            if not (self.tf_dir / ".terraform").exists():
                print(f"  Initializing Terraform in: {self.tf_dir}")
                subprocess.run(["terraform", "init"], cwd=self.tf_dir, check=True, capture_output=True)
        except Exception as e:
            print(f"  Failed to mirror Terraform modules: {e}")

    async def execute(self, action: str, resource_type: str, parameters: Dict[str, Any], env_vars: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        Execute Terraform command to modify infrastructure.
        Implements a 'Dry Run' (Validate -> Plan) before Apply.
        """
        print(f"  Terraform Execution: {action} {resource_type}")

        if action not in ["create", "update", "delete"]:
             return {"success": False, "error": f"Terraform service does not support action '{action}' directly."}

        # 1. Generate HCL
        hcl_code = await self._generate_hcl_with_llm(action, resource_type, parameters)
        if not hcl_code:
            return {"success": False, "error": "Failed to generate Terraform code."}
            
        # 2. Test the HCL in isolation before committing
        # We use a temporary file to validate the directory
        dynamic_tf_file = self.tf_dir / "dynamic_resources.tf"
        backup_content = ""
        if dynamic_tf_file.exists():
            with open(dynamic_tf_file, "r") as f:
                backup_content = f.read()

        try:
            # Append new HCL
            with open(dynamic_tf_file, "a" if backup_content else "w") as f:
                f.write(f"\n# Auto-generated for {resource_type}\n")
                f.write(hcl_code)
                f.write("\n")

            # 3. Dry Run Phase
            print("  [Dry Run] Validating HCL...")
            validate_res = await self._run_terraform_command("validate", {}, env_vars=env_vars)
            if not validate_res["success"]:
                # Revert if validation fails
                with open(dynamic_tf_file, "w") as f: f.write(backup_content)
                return {"success": False, "error": "HCL Validation failed", "details": validate_res["error"]}

            print("  [Dry Run] Planning changes...")
            plan_res = await self._run_terraform_command("plan", {}, env_vars=env_vars)
            if not plan_res["success"]:
                # Revert if planning fails
                with open(dynamic_tf_file, "w") as f: f.write(backup_content)
                return {"success": False, "error": "Terraform Plan failed", "details": plan_res["error"]}

            # 4. Final Apply
            print("  [Execution] Applying changes...")
            return await self._run_terraform_command("apply", {}, env_vars=env_vars)

        except Exception as e:
            # Revert on any file error
            if backup_content:
                with open(dynamic_tf_file, "w") as f: f.write(backup_content)
            return {"success": False, "error": f"Internal process error: {str(e)}"}

    async def _generate_hcl_with_llm(self, action: str, resource_type: str, parameters: Dict[str, Any]) -> Optional[str]:
        """Ask Ollama to write Terraform HCL."""
        try:
            prompt = f"""You are a Terraform HCL Expert. Write ONLY valid HCL code.
            
            Task: Write Terraform configuration to {action} an AWS {resource_type}.
            Parameters: {json.dumps(parameters)}
            
            Constraints:
            - Use 'aws' provider.
            - Do NOT include provider configuration.
            - Output ONLY the 'resource' block.
            - CRITICAL: Do NOT use 'var.VariableName'. Hardcode all values directly from the provided parameters.
            - Example: use bucket = "my-bucket" NOT bucket = var.bucket.
            - Ensure resource names are unique.
            
            Response must be ONLY valid HCL code. No markdown formatting.
            """
            
            response = self.model_router.generate(
                task="terraform_codegen",
                prompt=prompt,
                options={"temperature": 0},
            )
            hcl = response["response"].strip()
            
            # Clean markdown if present
            if "```hcl" in hcl:
                hcl = hcl.split("```hcl")[1].split("```")[0].strip()
            elif "```" in hcl:
                hcl = hcl.split("```")[1].split("```")[0].strip()
                
            print(f"  Generated HCL (preview): {hcl[:100]}...")
            return hcl
            
        except Exception as e:
            print(f"  HCL Generation failed: {e}")
            return None

    async def _run_terraform_command(self, command: str, vars_dict: Dict[str, Any], env_vars: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Run terraform plan/apply/destroy with variables and environment."""
        
        # Prepare environment
        run_env = os.environ.copy()
        if env_vars:
            run_env.update(env_vars)
        
        # Construct variable arguments
        var_args = []
        for k, v in vars_dict.items():
            var_args.extend(["-var", f"{k}={v}"])

        cmd = ["terraform", command, "-auto-approve"] + var_args
        # Parallelism default
        if command in ["plan", "apply"]:
             cmd.append("-parallelism=20")
        
        # If plan, remove -auto-approve
        if command == "plan":
            cmd = ["terraform", "plan", "-parallelism=20"] + var_args

        try:
            print(f"  Running: {' '.join(cmd)}")
            
            # Run in thread pool to not block async loop
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.tf_dir,
                env=run_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                return {
                    "success": True,
                    "output": stdout.decode(),
                    "message": f"Terraform {command} successful"
                }
            else:
                return {
                    "success": False,
                    "error": stderr.decode(),
                    "output": stdout.decode()
                }

        except Exception as e:
            return {"success": False, "error": str(e)}

    # Helper for legacy static mapping (kept for backward compat or specific modules)
    def _map_parameters_to_vars(self, resource_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return {}
