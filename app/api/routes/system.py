from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: Literal["ready"] = "ready"
    checks: dict[str, Literal["ok"]]


class VersionResponse(BaseModel):
    name: str = "moment-one-server"
    version: str = "0.1.0"


@router.get("/healthz", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@router.get("/readyz", response_model=ReadinessResponse)
async def readiness() -> ReadinessResponse:
    # External dependency checks will be added when connection details are available.
    return ReadinessResponse(checks={"application": "ok"})


@router.get("/version", response_model=VersionResponse)
async def version() -> VersionResponse:
    return VersionResponse()
