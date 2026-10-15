from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from quberi_api.core.config import get_settings
from quberi_api.health.schemas import LivenessResponse, ReadinessResponse
from quberi_api.health.service import get_readiness
from quberi_api.infrastructure.registry import Infrastructure

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=LivenessResponse)
async def liveness() -> LivenessResponse:
    settings = get_settings()
    return LivenessResponse(version=settings.app_version)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
async def readiness(request: Request) -> Any:
    settings = get_settings()
    infrastructure: Infrastructure = request.app.state.infrastructure
    result = await get_readiness(settings, infrastructure)
    if result.status == "unready":
        return JSONResponse(status_code=503, content=result.model_dump(mode="json"))
    return result
