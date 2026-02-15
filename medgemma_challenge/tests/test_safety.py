from medgemma_challenge.app.safety import enforce_medication_fidelity, enforce_red_flag_coverage
from medgemma_challenge.app.schemas import Medication, MedicationInstruction


def test_medication_fidelity_overrides_unsafe_changes():
    source = [
        Medication(name="Furosemide", dose="40 mg", frequency="once daily", purpose="fluid control"),
    ]
    generated = [
        MedicationInstruction(
            name="Furosemide",
            dose="80 mg",
            frequency="twice daily",
            purpose="fluid control",
            patient_instruction="Take extra when needed",
        )
    ]

    safe = enforce_medication_fidelity(source, generated)
    assert safe[0].dose == "40 mg"
    assert safe[0].frequency == "once daily"


def test_red_flag_coverage_merges_missing_flags():
    source = ["Chest pain", "Fever"]
    generated = ["Chest pain"]
    merged = enforce_red_flag_coverage(source, generated)
    assert "Chest pain" in merged
    assert "Fever" in merged

