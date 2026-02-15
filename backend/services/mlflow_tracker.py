import json
from typing import Any, Dict, Optional


class MLflowTracker:
    def __init__(
        self,
        enabled: bool,
        tracking_uri: str,
        experiment_name: str,
    ):
        self.enabled = bool(enabled)
        self.tracking_uri = str(tracking_uri or "").strip()
        self.experiment_name = str(experiment_name or "ai-infra-agent").strip() or "ai-infra-agent"
        self.available = False
        self._mlflow = None

    def initialize(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"success": True, "enabled": False, "available": False}
        try:
            import mlflow  # type: ignore

            if self.tracking_uri:
                mlflow.set_tracking_uri(self.tracking_uri)
            mlflow.set_experiment(self.experiment_name)
            self._mlflow = mlflow
            self.available = True
            return {
                "success": True,
                "enabled": True,
                "available": True,
                "tracking_uri": self.tracking_uri,
                "experiment_name": self.experiment_name,
            }
        except Exception as e:
            self.available = False
            return {
                "success": False,
                "enabled": self.enabled,
                "available": False,
                "error": str(e),
            }

    def log_request_run(
        self,
        request_id: str,
        user: Dict[str, Any],
        request_payload: Dict[str, Any],
        intent: Optional[Dict[str, Any]],
        execution_result: Optional[Dict[str, Any]],
        status: str,
        duration_ms: int,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"success": True, "skipped": "disabled"}
        if not self.available or self._mlflow is None:
            return {"success": False, "error": "MLflow not initialized"}

        mlflow = self._mlflow
        safe_intent = intent if isinstance(intent, dict) else {}
        safe_result = execution_result if isinstance(execution_result, dict) else {}

        try:
            run_name = f"req-{str(request_id)[:12]}"
            with mlflow.start_run(run_name=run_name):
                mlflow.set_tags(
                    {
                        "request_id": str(request_id),
                        "username": str(user.get("username") or "unknown"),
                        "role": str(user.get("role") or "user"),
                        "environment": str(request_payload.get("environment") or "dev"),
                        "cloud_provider": str(request_payload.get("cloud_provider") or "aws"),
                        "status": str(status or "unknown"),
                        "action": str(safe_intent.get("action") or "unknown"),
                        "resource_type": str(safe_intent.get("resource_type") or "unknown"),
                        "region": str(safe_intent.get("region") or request_payload.get("aws_region") or ""),
                        "execution_path": str(safe_result.get("execution_path") or "unknown"),
                    }
                )

                confidence = safe_intent.get("confidence")
                if isinstance(confidence, (int, float)):
                    mlflow.log_metric("intent_confidence", float(confidence))

                mlflow.log_metric("duration_ms", float(max(int(duration_ms or 0), 0)))
                mlflow.log_metric("success", 1.0 if bool(safe_result.get("success")) else 0.0)
                mlflow.log_metric("needs_input", 1.0 if bool(safe_result.get("requires_input")) else 0.0)

                outcome = safe_result.get("outcome_validation", {})
                summary = outcome.get("summary", {}) if isinstance(outcome, dict) else {}
                if isinstance(summary, dict):
                    for key in ["total", "passed", "failed", "pending", "skipped"]:
                        val = summary.get(key)
                        if isinstance(val, (int, float)):
                            mlflow.log_metric(f"outcome_{key}", float(val))

                req_text = str(request_payload.get("natural_language_request") or "")
                mlflow.log_param("request_text_len", len(req_text))
                mlflow.log_param("has_input_variables", bool(request_payload.get("input_variables")))

                request_artifact = {
                    "request_id": request_id,
                    "requester": user.get("username"),
                    "payload": {
                        "natural_language_request": request_payload.get("natural_language_request"),
                        "environment": request_payload.get("environment"),
                        "cloud_provider": request_payload.get("cloud_provider"),
                        "aws_region": request_payload.get("aws_region"),
                        "input_variables": request_payload.get("input_variables"),
                    },
                }
                mlflow.log_text(json.dumps(request_artifact, ensure_ascii=True, indent=2), "request/request.json")
                mlflow.log_text(json.dumps(safe_intent, ensure_ascii=True, indent=2), "result/intent.json")
                mlflow.log_text(json.dumps(safe_result, ensure_ascii=True, indent=2), "result/execution_result.json")
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
