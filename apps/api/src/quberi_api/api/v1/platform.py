from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from quberi_api.core.config import get_settings

router = APIRouter(prefix="/platform", tags=["platform"])


class Capability(BaseModel):
    key: str
    status: Literal["available", "planned"]


class PlatformInfo(BaseModel):
    name: str
    display_name: str
    version: str
    api_version: str
    environment: str
    default_locale: str
    slice: str
    capabilities: list[Capability]


@router.get("", response_model=PlatformInfo)
async def platform_info() -> PlatformInfo:
    settings = get_settings()
    return PlatformInfo(
        name="quberi",
        display_name="Quberi",
        version=settings.app_version,
        api_version=settings.api_version,
        environment=settings.environment,
        default_locale=settings.default_locale,
        slice="foundation",
        capabilities=[
            Capability(key="platform_status", status="available"),
            Capability(key="knowledge", status="planned"),
            Capability(key="agents", status="planned"),
            Capability(key="workflows", status="planned"),
            Capability(key="data_foundation", status="planned"),
        ],
    )
