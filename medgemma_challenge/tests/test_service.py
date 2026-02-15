from medgemma_challenge.app.config import Settings
from medgemma_challenge.app.schemas import DischargePlanRequest
from medgemma_challenge.app.service import DischargeInstructionService


def test_service_generates_required_sections():
    settings = Settings(model_backend="mock")
    service = DischargeInstructionService(settings)
    request = DischargePlanRequest(
        patient_age=60,
        primary_diagnosis="Heart failure exacerbation",
        comorbidities=["Hypertension"],
        discharge_summary="Improved with treatment and stable for discharge.",
        medications=[
            {
                "name": "Furosemide",
                "dose": "40 mg",
                "frequency": "once daily",
                "purpose": "fluid control",
            }
        ],
        follow_up_instructions=["Cardiology follow-up in 1 week"],
        red_flags=["Chest pain", "Shortness of breath"],
        target_language="english",
        health_literacy_level="basic",
    )

    response = service.generate(request)
    assert response.plain_language_summary
    assert len(response.medication_schedule) == 1
    assert "Chest pain" in response.red_flags
    assert response.metadata.backend_used == "mock"
