"""OAuth 2.1 Token 端点（眼镜端接口，无 Casdoor 鉴权）。

支持两种 grant_type：
- urn:momentone:oauth:grant-type:qr-binding: 眼镜端扫码绑定
- refresh_token: 刷新 access_token（滚动续期）

遵循 OAuth 2.1 Token 端点规范（RFC 6749 §5）。
"""

from fastapi import APIRouter, Depends, Form
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.device_repository import (
    SqlBindingCodeRepository,
    SqlDeviceBindingRepository,
    SqlDeviceRepository,
)
from app.infrastructure.database.session import get_db_session
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.devices.service import DeviceBindingService

router = APIRouter(prefix="/oauth", tags=["oauth"])

QR_BINDING_GRANT_TYPE = "urn:momentone:oauth:grant-type:qr-binding"
REFRESH_GRANT_TYPE = "refresh_token"


def _get_jwt_issuer(settings: Settings = Depends(get_settings)) -> JwtIssuer:
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
    )


class TokenResponse(BaseModel):
    """OAuth 2.1 Token 端点响应（RFC 6749 §5.1）。"""

    binding_id: str = Field(alias="binding_id")
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str

    model_config = {"populate_by_name": True}


class TokenError(BaseModel):
    """OAuth 2.1 Token 端点错误响应（RFC 6749 §5.2）。"""

    error: str
    error_description: str


@router.post("/token", response_model=TokenResponse)
async def token(
    grant_type: str = Form(...),
    binding_code: str | None = Form(default=None),
    device_id: str | None = Form(default=None),
    device_name: str | None = Form(default=None),
    device_type: str | None = Form(default=None),
    refresh_token: str | None = Form(default=None),
    service: DeviceBindingService = Depends(_make_service),
) -> TokenResponse:
    if grant_type == QR_BINDING_GRANT_TYPE:
        if not binding_code or not device_id:
            raise ApplicationError(
                code="INVALID_REQUEST",
                message="binding_code 和 device_id 是必需的。",
                status_code=400,
            )
        result = await service.complete_binding(
            binding_code=binding_code,
            device_id=device_id,
            device_name=device_name,
            device_type=device_type,
        )
        return TokenResponse(
            binding_id=str(result.binding_id),
            access_token=result.access_token,
            refresh_token=result.refresh_token,
            token_type=result.token_type,
            expires_in=result.expires_in,
            scope=result.scope,
        )

    if grant_type == REFRESH_GRANT_TYPE:
        if not refresh_token:
            raise ApplicationError(
                code="INVALID_REQUEST",
                message="refresh_token 是必需的。",
                status_code=400,
            )
        result = await service.refresh_access_token(refresh_token)
        return TokenResponse(
            binding_id=str(result.binding_id),
            access_token=result.access_token,
            refresh_token=result.refresh_token,
            token_type=result.token_type,
            expires_in=result.expires_in,
            scope=result.scope,
        )

    raise ApplicationError(
        code="UNSUPPORTED_GRANT_TYPE",
        message=f"不支持的 grant_type: {grant_type}",
        status_code=400,
    )
