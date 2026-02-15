from typing import List

from .schemas import Medication, MedicationInstruction


def _norm(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def enforce_medication_fidelity(
    source_medications: List[Medication],
    generated_schedule: List[MedicationInstruction],
) -> List[MedicationInstruction]:
    source_by_name = {_norm(med.name): med for med in source_medications}
    generated_by_name = {_norm(med.name): med for med in generated_schedule}

    safe_schedule: List[MedicationInstruction] = []
    for source_key, source_med in source_by_name.items():
        generated = generated_by_name.get(source_key)
        if generated:
            safe_schedule.append(
                MedicationInstruction(
                    name=source_med.name,
                    dose=source_med.dose,
                    frequency=source_med.frequency,
                    purpose=(generated.purpose or source_med.purpose or "").strip(),
                    patient_instruction=(
                        generated.patient_instruction
                        or f"Take {source_med.name} exactly as prescribed."
                    ).strip(),
                )
            )
        else:
            safe_schedule.append(
                MedicationInstruction(
                    name=source_med.name,
                    dose=source_med.dose,
                    frequency=source_med.frequency,
                    purpose=(source_med.purpose or "").strip(),
                    patient_instruction=f"Take {source_med.name} exactly as prescribed.",
                )
            )

    return safe_schedule


def enforce_red_flag_coverage(source_red_flags: List[str], generated_red_flags: List[str]) -> List[str]:
    merged: List[str] = []
    seen = set()
    for item in generated_red_flags + source_red_flags:
        token = str(item or "").strip()
        key = token.lower()
        if token and key not in seen:
            seen.add(key)
            merged.append(token)
    return merged


def detect_safety_warnings(summary_text: str, follow_up_plan: List[str]) -> List[str]:
    warnings: List[str] = []
    summary_lower = str(summary_text or "").lower()
    follow_text = " ".join(follow_up_plan).lower()

    unsafe_phrases = [
        "stop all medications",
        "double your dose",
        "ignore chest pain",
        "skip follow-up",
    ]
    for phrase in unsafe_phrases:
        if phrase in summary_lower or phrase in follow_text:
            warnings.append(f"Potential unsafe phrase detected: {phrase}")

    if not follow_up_plan:
        warnings.append("No follow-up plan generated.")

    return warnings

