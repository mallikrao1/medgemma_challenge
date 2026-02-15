from __future__ import annotations

from typing import Dict, List

from .config import Settings
from .metrics import flesch_reading_ease
from .model_backend import MedGemmaBackend, parse_json_object
from .prompting import build_generation_prompt
from .safety import (
    detect_safety_warnings,
    enforce_medication_fidelity,
    enforce_red_flag_coverage,
)
from .schemas import (
    DischargePlanRequest,
    DischargePlanResponse,
    MedicationInstruction,
    ResponseMetadata,
)
from .translation import translate_fallback


class DischargeInstructionService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.backend = MedGemmaBackend(settings)

    def generate(self, request: DischargePlanRequest) -> DischargePlanResponse:
        baseline = self._build_deterministic_plan(request)
        prompt = build_generation_prompt(request)
        model_result = self.backend.generate(prompt)
        parsed = parse_json_object(model_result.text)
        merged = self._merge_output(baseline, parsed)

        safe_schedule = enforce_medication_fidelity(request.medications, merged["medication_schedule"])
        safe_red_flags = enforce_red_flag_coverage(request.red_flags, merged["red_flags"])
        translated_summary = merged["translated_summary"] or translate_fallback(
            merged["plain_language_summary"], request.target_language
        )

        safety_warnings = detect_safety_warnings(
            merged["plain_language_summary"],
            merged["follow_up_plan"],
        )
        if model_result.error:
            safety_warnings.append(f"Model backend issue: {model_result.error}")

        readability = flesch_reading_ease(merged["plain_language_summary"])
        metadata = ResponseMetadata(
            backend_used=model_result.backend_used,
            model_id=model_result.model_id,
            generation_seconds=model_result.generation_seconds,
            readability_flesch=readability,
            safety_warnings=safety_warnings,
        )

        return DischargePlanResponse(
            plain_language_summary=merged["plain_language_summary"],
            translated_summary=translated_summary,
            medication_schedule=safe_schedule,
            red_flags=safe_red_flags,
            follow_up_plan=merged["follow_up_plan"],
            metadata=metadata,
        )

    def _merge_output(self, baseline: Dict, parsed: Dict | None) -> Dict:
        if not parsed:
            return baseline

        merged = dict(baseline)
        merged["plain_language_summary"] = str(
            parsed.get("plain_language_summary") or baseline["plain_language_summary"]
        ).strip()
        merged["translated_summary"] = str(parsed.get("translated_summary") or "").strip()
        merged["red_flags"] = _as_str_list(parsed.get("red_flags"), baseline["red_flags"])
        merged["follow_up_plan"] = _as_str_list(
            parsed.get("follow_up_plan"), baseline["follow_up_plan"]
        )

        med_items = parsed.get("medication_schedule") or []
        schedule: List[MedicationInstruction] = []
        if isinstance(med_items, list):
            for item in med_items:
                if not isinstance(item, dict):
                    continue
                try:
                    schedule.append(
                        MedicationInstruction(
                            name=str(item.get("name") or "").strip(),
                            dose=str(item.get("dose") or "").strip(),
                            frequency=str(item.get("frequency") or "").strip(),
                            purpose=str(item.get("purpose") or "").strip(),
                            patient_instruction=str(item.get("patient_instruction") or "").strip(),
                        )
                    )
                except Exception:
                    continue
        if schedule:
            merged["medication_schedule"] = schedule
        return merged

    def _build_deterministic_plan(self, request: DischargePlanRequest) -> Dict:
        diagnosis = request.primary_diagnosis.strip()
        first_sentence = (
            f"You were treated for {diagnosis}. "
            f"Please follow the plan below and ask for help if symptoms get worse."
        )
        if request.health_literacy_level == "advanced":
            first_sentence = (
                f"Discharge diagnosis: {diagnosis}. "
                "Below is your post-discharge management plan."
            )

        summary_parts = [first_sentence]
        if request.comorbidities:
            summary_parts.append(
                "Other health conditions noted: " + ", ".join(request.comorbidities[:5]) + "."
            )
        summary_parts.append("Take medications exactly as prescribed.")
        plain_summary = " ".join(summary_parts)

        schedule = []
        for med in request.medications:
            schedule.append(
                MedicationInstruction(
                    name=med.name,
                    dose=med.dose,
                    frequency=med.frequency,
                    purpose=med.purpose or "",
                    patient_instruction=f"Take {med.name} ({med.dose}) {med.frequency}.",
                )
            )

        follow_up = list(request.follow_up_instructions) or [
            "Follow up with your primary care clinician within 7 days.",
            "Bring your medication list to the next appointment.",
        ]

        red_flags = list(request.red_flags) or [
            "Chest pain",
            "Shortness of breath",
            "Fainting",
            "Persistent fever",
        ]

        return {
            "plain_language_summary": plain_summary,
            "translated_summary": translate_fallback(plain_summary, request.target_language),
            "medication_schedule": schedule,
            "red_flags": red_flags,
            "follow_up_plan": follow_up,
        }


def _as_str_list(value, fallback: List[str]) -> List[str]:
    if not isinstance(value, list):
        return fallback
    output = []
    for item in value:
        token = str(item or "").strip()
        if token:
            output.append(token)
    return output or fallback

