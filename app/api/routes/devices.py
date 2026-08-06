"""Web 端设备管理接口（Casdoor Bearer 鉴权）。

- POST /v1/device/bindings: 创建绑定会话，返回 binding_code + qr_payload
- GET /v1/device/bindings: 查看已绑定设备列表
- DELETE /v1/device/bindings/{id}: 撤销绑定
- PATCH /v1/device/bindings/{id}: 调整 scope
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_authenticated_user_id
from app.core.config import Settings, get_settings
from app.infrastructure.database.repositories.device_repository import (
    SqlBindingCodeRepository,
    SqlDeviceBindingRepository,
    SqlDeviceRepository,
)
from app.infrastructure.database.session import get_db_session
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.devices.domain import DeviceBinding
from app.modules.devices.service import DeviceBindingService
from app.modules.mcp_oauth.repositories import McpAuthorizationRepository

router = APIRouter(prefix="/v1/device", tags=["devices"])

QR_PAYLOAD_SCHEME = "momentone"
QR_PAYLOAD_HOST = "bind"


async def _get_user_id(
    user_id: UUID = Depends(get_authenticated_user_id),
) -> UUID:
    """鉴权依赖：支持 Casdoor OIDC 和眼镜端 JWT 双通道。

    设备管理接口理论上仅 Web 端使用（Casdoor），但接受眼镜端 JWT
    可让眼镜端查询自身绑定状态，不破坏安全模型。
    """
    return user_id


def _get_jwt_issuer(settings: Settings = Depends(get_settings)) -> JwtIssuer:
    """获取 JwtIssuer 单例。私钥/公钥在首次使用时加载。"""
    return JwtIssuer(settings)


def _make_service(
    settings: Settings = Depends(get_settings),
    jwt_issuer: JwtIssuer = Depends(_get_jwt_issuer),
    session: AsyncSession = Depends(get_db_session),
) -> DeviceBindingService:
    return DeviceBindingService(
        bindings=SqlDeviceBindingRepository(session),
        codes=SqlBindingCodeRepository(session),
        devices=SqlDeviceRepository(session),
        jwt_issuer=jwt_issuer,
        settings=settings,
        authorizations=McpAuthorizationRepository(session),
    )


# ---- Request/Response Models ----


class CreateBindingRequest(BaseModel):
    device_name: str | None = Field(default=None, max_length=120)
    scope: list[str] | None = None


class BindingSessionResponse(BaseModel):
    binding_code: str
    qr_payload: str
    expires_at: str  # ISO 8601
    scope: list[str]


class DeviceBindingResponse(BaseModel):
    id: str
    device_id: str
    scope: list[str]
    status: str
    bound_at: str
    last_active_at: str | None
    revoked_at: str | None


class UpdateScopeRequest(BaseModel):
    scope: list[str] = Field(min_length=1)


def _binding_to_response(b: DeviceBinding) -> DeviceBindingResponse:
    return DeviceBindingResponse(
        id=str(b.id),
        device_id=b.device_id,
        scope=list(b.scope),
        status=b.status.value,
        bound_at=b.bound_at.isoformat(),
        last_active_at=b.last_active_at.isoformat() if b.last_active_at else None,
        revoked_at=b.revoked_at.isoformat() if b.revoked_at else None,
    )


# ---- Routes ----


@router.post(
    "/bindings", response_model=BindingSessionResponse, status_code=status.HTTP_201_CREATED
)
async def create_binding_session(
    body: CreateBindingRequest,
    user_id: UUID = Depends(_get_user_id),
    service: DeviceBindingService = Depends(_make_service),
) -> BindingSessionResponse:
    scope = tuple(body.scope) if body.scope else None
    code = await service.create_binding_session(
        user_id=user_id,
        scope=scope,
        device_name=body.device_name,
    )
    qr_payload = f"{QR_PAYLOAD_SCHEME}://{QR_PAYLOAD_HOST}?code={code.code}"
    return BindingSessionResponse(
        binding_code=code.code,
        qr_payload=qr_payload,
        expires_at=code.expires_at.isoformat(),
        scope=list(code.scope),
    )


@router.get("/bindings", response_model=list[DeviceBindingResponse])
async def list_bindings(
    user_id: UUID = Depends(_get_user_id),
    service: DeviceBindingService = Depends(_make_service),
) -> list[DeviceBindingResponse]:
    bindings = await service.list_user_bindings(user_id)
    return [_binding_to_response(b) for b in bindings]


@router.delete("/bindings/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_binding(
    binding_id: UUID,
    user_id: UUID = Depends(_get_user_id),
    service: DeviceBindingService = Depends(_make_service),
) -> None:
    await service.revoke_binding(user_id=user_id, binding_id=binding_id)


@router.patch("/bindings/{binding_id}", response_model=DeviceBindingResponse)
async def update_binding_scope(
    binding_id: UUID,
    body: UpdateScopeRequest,
    user_id: UUID = Depends(_get_user_id),
    service: DeviceBindingService = Depends(_make_service),
) -> DeviceBindingResponse:
    binding = await service.update_scope(
        user_id=user_id,
        binding_id=binding_id,
        scope=tuple(body.scope),
    )
    return _binding_to_response(binding)
