from fastapi import APIRouter

from ..config import settings
from ..schemas import HealthResponse


router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return HealthResponse(status="healthy", app=settings.app_name, version=settings.app_version)

