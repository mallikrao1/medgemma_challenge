from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .schemas import DischargePlanRequest, DischargePlanResponse
from .service import DischargeInstructionService


service = DischargeInstructionService(settings)
app = FastAPI(title=settings.app_name, version=settings.app_version)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/demo-assets", StaticFiles(directory=str(FRONTEND_DIR)), name="demo-assets")


@app.get("/health")
def health() -> dict:
    return {
        "status": "healthy",
        "app": settings.app_name,
        "version": settings.app_version,
        "backend": settings.model_backend,
        "model": settings.medgemma_model_id,
    }


@app.get("/")
def demo_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.post("/api/v1/discharge-plan", response_model=DischargePlanResponse)
def generate_discharge_plan(payload: DischargePlanRequest) -> DischargePlanResponse:
    return service.generate(payload)

