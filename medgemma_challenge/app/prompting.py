import json

from .schemas import DischargePlanRequest


def build_generation_prompt(request: DischargePlanRequest) -> str:
    instructions = (
        "You are a careful clinical discharge communication assistant. "
        "Generate patient-safe instructions without changing medication dose or frequency. "
        "If uncertain, state uncertainty. Do not invent diagnoses, labs, or medications."
    )

    schema_hint = {
        "plain_language_summary": "string",
        "translated_summary": "string in target language",
        "medication_schedule": [
            {
                "name": "string",
                "dose": "string (must match source)",
                "frequency": "string (must match source)",
                "purpose": "string",
                "patient_instruction": "string",
            }
        ],
        "red_flags": ["string"],
        "follow_up_plan": ["string"],
    }

    payload = {
        "patient_age": request.patient_age,
        "primary_diagnosis": request.primary_diagnosis,
        "comorbidities": request.comorbidities,
        "discharge_summary": request.discharge_summary,
        "medications": [med.model_dump() for med in request.medications],
        "follow_up_instructions": request.follow_up_instructions,
        "red_flags": request.red_flags,
        "target_language": request.target_language,
        "health_literacy_level": request.health_literacy_level,
    }

    return (
        f"{instructions}\n\n"
        "Output format requirements:\n"
        "1) Return only valid JSON.\n"
        "2) Include every red flag from the source input.\n"
        "3) Keep language simple, short sentences.\n\n"
        f"JSON schema:\n{json.dumps(schema_hint, ensure_ascii=True, indent=2)}\n\n"
        f"Input:\n{json.dumps(payload, ensure_ascii=True, indent=2)}\n"
    )

