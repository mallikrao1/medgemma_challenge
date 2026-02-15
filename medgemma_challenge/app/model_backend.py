import json
import time
from dataclasses import dataclass
from typing import Optional

import requests

from .config import Settings


@dataclass
class GenerationResult:
    text: str
    backend_used: str
    model_id: str
    generation_seconds: float
    error: str = ""


class MedGemmaBackend:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._transformers_ready = False
        self._tokenizer = None
        self._model = None
        self._torch = None
        self._device = "cpu"

    def generate(self, prompt: str) -> GenerationResult:
        backend = (self.settings.model_backend or "mock").strip().lower()
        if backend == "transformers":
            return self._generate_transformers(prompt)
        if backend == "openai_compatible":
            return self._generate_openai_compatible(prompt)
        return GenerationResult(
            text="",
            backend_used="mock",
            model_id=self.settings.medgemma_model_id,
            generation_seconds=0.0,
        )

    def _generate_transformers(self, prompt: str) -> GenerationResult:
        started = time.perf_counter()
        try:
            self._ensure_transformers_loaded()
            encoded = self._tokenizer(
                prompt,
                return_tensors="pt",
                truncation=True,
                max_length=4096,
            )
            if self._device != "cpu":
                encoded = {k: v.to(self._device) for k, v in encoded.items()}

            do_sample = float(self.settings.temperature) > 0.0
            kwargs = {
                "max_new_tokens": int(self.settings.max_new_tokens),
                "do_sample": do_sample,
            }
            if do_sample:
                kwargs["temperature"] = float(self.settings.temperature)

            with self._torch.no_grad():
                output = self._model.generate(**encoded, **kwargs)
            raw = self._tokenizer.decode(output[0], skip_special_tokens=True)

            if raw.startswith(prompt):
                raw = raw[len(prompt) :].strip()
            return GenerationResult(
                text=raw,
                backend_used="transformers",
                model_id=self.settings.medgemma_model_id,
                generation_seconds=round(time.perf_counter() - started, 3),
            )
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            return GenerationResult(
                text="",
                backend_used="transformers",
                model_id=self.settings.medgemma_model_id,
                generation_seconds=round(time.perf_counter() - started, 3),
                error=str(exc),
            )

    def _ensure_transformers_loaded(self) -> None:
        if self._transformers_ready:
            return

        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._torch = torch
        if torch.cuda.is_available():
            self._device = "cuda"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            self._device = "mps"
        else:
            self._device = "cpu"

        self._tokenizer = AutoTokenizer.from_pretrained(self.settings.medgemma_model_id)
        self._model = AutoModelForCausalLM.from_pretrained(self.settings.medgemma_model_id)
        self._model.to(self._device)
        self._model.eval()
        self._transformers_ready = True

    def _generate_openai_compatible(self, prompt: str) -> GenerationResult:
        started = time.perf_counter()
        base_url = self.settings.openai_base_url.strip().rstrip("/")
        api_key = self.settings.openai_api_key.strip()
        model = self.settings.openai_model.strip() or self.settings.medgemma_model_id

        if not base_url:
            return GenerationResult(
                text="",
                backend_used="openai_compatible",
                model_id=model,
                generation_seconds=0.0,
                error="OPENAI_BASE_URL is not configured",
            )

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": float(self.settings.temperature),
            "max_tokens": int(self.settings.max_new_tokens),
        }

        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers=headers,
                data=json.dumps(payload),
                timeout=int(self.settings.timeout_seconds),
            )
            response.raise_for_status()
            data = response.json()
            text = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            return GenerationResult(
                text=text,
                backend_used="openai_compatible",
                model_id=model,
                generation_seconds=round(time.perf_counter() - started, 3),
            )
        except Exception as exc:  # pragma: no cover - runtime fallback
            return GenerationResult(
                text="",
                backend_used="openai_compatible",
                model_id=model,
                generation_seconds=round(time.perf_counter() - started, 3),
                error=str(exc),
            )


def parse_json_object(raw_text: str) -> Optional[dict]:
    text = (raw_text or "").strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    snippet = text[start : end + 1]
    try:
        return json.loads(snippet)
    except json.JSONDecodeError:
        return None

