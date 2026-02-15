from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class Medication(BaseModel):
    name: str = Field(..., min_length=1)
    dose: str = Field(..., min_length=1)
    frequency: str = Field(..., min_length=1)
    purpose: Optional[str] = ""


class MedicationInstruction(Medication):
    patient_instruction: str = Field(default="")


class DischargePlanRequest(BaseModel):
    patient_age: int = Field(..., ge=0, le=120)
    primary_diagnosis: str = Field(..., min_length=3)
    comorbidities: List[str] = Field(default_factory=list)
    discharge_summary: str = Field(..., min_length=20)
    medications: List[Medication] = Field(default_factory=list)
    follow_up_instructions: List[str] = Field(default_factory=list)
    red_flags: List[str] = Field(default_factory=list)
    target_language: str = Field(default="english")
    health_literacy_level: str = Field(default="basic")

    @field_validator("target_language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        return normalized or "english"

    @field_validator("health_literacy_level")
    @classmethod
    def normalize_literacy(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"basic", "intermediate", "advanced"}:
            return "basic"
        return normalized


class ResponseMetadata(BaseModel):
    backend_used: str
    model_id: str
    generation_seconds: float
    readability_flesch: float
    safety_warnings: List[str] = Field(default_factory=list)


class DischargePlanResponse(BaseModel):
    plain_language_summary: str
    translated_summary: str
    medication_schedule: List[MedicationInstruction] = Field(default_factory=list)
    red_flags: List[str] = Field(default_factory=list)
    follow_up_plan: List[str] = Field(default_factory=list)
    metadata: ResponseMetadata

