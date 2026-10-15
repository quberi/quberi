from typing import Literal

from pydantic import BaseModel


class ComponentHealth(BaseModel):
    name: str
    status: Literal["up", "down"]
    latency_ms: float


class ReadinessResponse(BaseModel):
    status: Literal["ready", "unready"]
    version: str
    components: list[ComponentHealth]


class LivenessResponse(BaseModel):
    status: Literal["alive"] = "alive"
    version: str
