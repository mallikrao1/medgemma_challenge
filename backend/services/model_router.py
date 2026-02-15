"""
Model Router for Ollama tasks.
Routes task types to preferred models with automatic fallback.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Any
import json
import random
import time
import re

from config import settings

try:
    import ollama
    OLLAMA_AVAILABLE = True
except ImportError:  # pragma: no cover
    OLLAMA_AVAILABLE = False


def _split_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


class ModelRouter:
    def __init__(self):
        self.profile = str(settings.MODEL_ROUTER_PROFILE or "balanced").strip().lower()
        self.scorer_enabled = bool(
            settings.MODEL_ROUTER_ENABLE_SCORER
            and self.profile in {"auto", "auto_hybrid", "glm5_auto", "glm-5-auto"}
        )
        self.exploration_rate = max(0.0, min(1.0, float(settings.MODEL_ROUTER_SCORER_EXPLORATION)))
        self.quality_weight = max(0.0, float(settings.MODEL_ROUTER_QUALITY_WEIGHT))
        self.latency_weight = max(0.0, float(settings.MODEL_ROUTER_LATENCY_WEIGHT))
        self.failure_weight = max(0.0, float(settings.MODEL_ROUTER_FAILURE_WEIGHT))
        self.ema_alpha = max(0.01, min(0.99, float(settings.MODEL_ROUTER_EMA_ALPHA)))
        self.model_stats: Dict[str, Dict[str, Dict[str, float]]] = {}
        self.routes = self._build_routes()

    def _build_routes(self) -> Dict[str, List[str]]:
        glm5_profile = self.profile in {
            "glm5",
            "glm5_hybrid",
            "glm-5",
            "glm-5-hybrid",
            "auto",
            "auto_hybrid",
            "glm5_auto",
            "glm-5-auto",
        }

        nlu_prefix = _split_csv(settings.GLM5_NLU_MODELS) if glm5_profile else []
        codegen_prefix = _split_csv(settings.GLM5_CODEGEN_MODELS) if glm5_profile else []
        verifier_prefix = _split_csv(settings.GLM5_VERIFIER_MODELS) if glm5_profile else []

        nlu_models = _dedupe_keep_order(
            nlu_prefix
            + [settings.NLU_PRIMARY_MODEL]
            + _split_csv(settings.NLU_FALLBACK_MODELS)
            + [settings.OLLAMA_ORCHESTRATOR_MODEL]
        )
        codegen_models = _dedupe_keep_order(
            codegen_prefix
            + [settings.CODEGEN_PRIMARY_MODEL]
            + _split_csv(settings.CODEGEN_FALLBACK_MODELS)
            + [settings.OLLAMA_CODEGEN_MODEL]
        )
        verifier_models = _dedupe_keep_order(
            verifier_prefix
            + [settings.VERIFIER_MODEL or settings.NLU_PRIMARY_MODEL]
            + _split_csv(settings.VERIFIER_FALLBACK_MODELS)
            + nlu_models
        )
        return {
            "nlu": nlu_models,
            "intent_verifier": verifier_models,
            "codegen": codegen_models,
            "codegen_repair": codegen_models,
            "terraform_codegen": codegen_models,
        }

    def get_models_for_task(self, task: str) -> List[str]:
        models = self.routes.get(task)
        if models:
            return models
        return self.routes.get("nlu", [])

    def _task_stats(self, task: str) -> Dict[str, Dict[str, float]]:
        bucket = self.model_stats.get(task)
        if bucket is None:
            bucket = {}
            self.model_stats[task] = bucket
        return bucket

    def _model_stats(self, task: str, model: str) -> Dict[str, float]:
        bucket = self._task_stats(task)
        stats = bucket.get(model)
        if stats is None:
            stats = {
                "calls": 0.0,
                "successes": 0.0,
                "failures": 0.0,
                "ema_latency_ms": 0.0,
                "ema_quality": 0.55,
            }
            bucket[model] = stats
        return stats

    def _safe_json_parse(self, text: str) -> bool:
        if not text:
            return False
        cleaned = str(text).strip()
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
            return isinstance(parsed, (dict, list))
        except Exception:
            return False

    def _estimate_quality(
        self,
        task: str,
        response_text: str,
        output_format: Optional[str],
    ) -> float:
        text = str(response_text or "").strip()
        if not text:
            return 0.0

        if output_format == "json" or task in {"nlu", "intent_verifier"}:
            return 1.0 if self._safe_json_parse(text) else 0.35

        lowered = text.lower()
        if task in {"codegen", "codegen_repair", "terraform_codegen"}:
            code_signals = [
                "import boto3",
                "boto3.client(",
                "def ",
                "try:",
                "except ",
                "terraform",
                "resource ",
                "provider ",
            ]
            signal_hits = sum(1 for token in code_signals if token in lowered)
            return min(1.0, 0.45 + (0.08 * signal_hits))

        # Generic response quality heuristic.
        length_score = min(1.0, len(text) / 180.0)
        structure_bonus = 0.2 if re.search(r"[{}[\]:]", text) else 0.0
        return min(1.0, 0.35 + (0.45 * length_score) + structure_bonus)

    def _priority_bias(self, task: str, model: str) -> float:
        model_l = str(model or "").lower()
        if task in {"nlu", "intent_verifier"}:
            if "qwen2.5:14b-instruct" in model_l:
                return 0.06
            if "glm-5" in model_l:
                return 0.04
        if task in {"codegen", "codegen_repair", "terraform_codegen"}:
            if "qwen2.5-coder:14b" in model_l:
                return 0.08
            if "glm-5" in model_l:
                return 0.05
        if "llama3.2:3b" in model_l:
            return -0.02
        return 0.0

    def _latency_score(self, latency_ms: float) -> float:
        if latency_ms <= 0:
            return 0.6
        # 1.0 for very fast, decays smoothly as latency grows.
        return max(0.0, min(1.0, 1.0 / (1.0 + (latency_ms / 2500.0))))

    def _model_runtime_score(self, task: str, model: str) -> float:
        stats = self._model_stats(task, model)
        calls = max(0.0, stats.get("calls", 0.0))
        if calls <= 0:
            # Encourage exploration for unseen models.
            return 1.5 + self._priority_bias(task, model)

        quality = max(0.0, min(1.0, stats.get("ema_quality", 0.55)))
        latency_component = self._latency_score(stats.get("ema_latency_ms", 0.0))
        failure_rate = min(1.0, stats.get("failures", 0.0) / max(1.0, calls))

        return (
            (self.quality_weight * quality)
            + (self.latency_weight * latency_component)
            - (self.failure_weight * failure_rate)
            + self._priority_bias(task, model)
        )

    def _rank_models(self, task: str, models: List[str]) -> List[str]:
        if not self.scorer_enabled or len(models) <= 1:
            return models

        ranked = sorted(
            models,
            key=lambda item: self._model_runtime_score(task, item),
            reverse=True,
        )

        # Occasional exploration so the scorer keeps learning.
        if random.random() < self.exploration_rate:
            random.shuffle(ranked)
        return ranked

    def _update_stats(
        self,
        task: str,
        model: str,
        success: bool,
        latency_ms: float,
        quality: float = 0.0,
    ):
        stats = self._model_stats(task, model)
        calls = stats.get("calls", 0.0) + 1.0
        stats["calls"] = calls
        if success:
            stats["successes"] = stats.get("successes", 0.0) + 1.0
        else:
            stats["failures"] = stats.get("failures", 0.0) + 1.0

        prev_latency = stats.get("ema_latency_ms", 0.0)
        if prev_latency <= 0:
            stats["ema_latency_ms"] = max(1.0, latency_ms)
        else:
            stats["ema_latency_ms"] = (
                (self.ema_alpha * max(1.0, latency_ms))
                + ((1.0 - self.ema_alpha) * prev_latency)
            )

        prev_quality = stats.get("ema_quality", 0.55)
        bounded_quality = max(0.0, min(1.0, quality))
        stats["ema_quality"] = (
            (self.ema_alpha * bounded_quality)
            + ((1.0 - self.ema_alpha) * prev_quality)
        )

    def generate(
        self,
        task: str,
        prompt: str,
        system: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
        format: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not OLLAMA_AVAILABLE:
            raise RuntimeError("Ollama client is not available.")

        models = self.get_models_for_task(task)
        if not models:
            raise RuntimeError(f"No models configured for task '{task}'.")
        models = self._rank_models(task, models)

        errors: List[str] = []
        opts = {"temperature": 0}
        if isinstance(options, dict):
            opts.update(options)

        for model in models:
            started = time.perf_counter()
            try:
                kwargs: Dict[str, Any] = {
                    "model": model,
                    "prompt": prompt,
                    "options": opts,
                }
                if system:
                    kwargs["system"] = system
                if format:
                    kwargs["format"] = format

                try:
                    response = ollama.generate(**kwargs)
                except TypeError as e:
                    if format and "format" in str(e).lower():
                        kwargs.pop("format", None)
                        response = ollama.generate(**kwargs)
                    else:
                        raise
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                response_text = str(response.get("response", ""))
                quality = self._estimate_quality(task, response_text, format)
                self._update_stats(task, model, success=True, latency_ms=elapsed_ms, quality=quality)
                response["model_used"] = model
                if self.scorer_enabled:
                    response["model_selection"] = {
                        "task": task,
                        "profile": self.profile,
                        "selected_model": model,
                        "latency_ms": round(elapsed_ms, 2),
                        "quality_score": round(quality, 3),
                        "candidate_order": models,
                    }
                return response
            except Exception as e:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                self._update_stats(task, model, success=False, latency_ms=elapsed_ms, quality=0.0)
                errors.append(f"{model}: {e}")
                continue

        raise RuntimeError(
            f"All models failed for task '{task}'. Tried: {', '.join(models)}. "
            f"Errors: {' | '.join(errors[:3])}"
        )
