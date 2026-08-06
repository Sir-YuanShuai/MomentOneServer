"""OAuth 2.1 Token 端点（眼镜端 + MCP OAuth 共用）。

支持三种 grant_type：
- urn:momentone:oauth:grant-type:qr-binding: 眼镜端扫码绑定
- authorization_code: MCP OAuth 授权码换 token（PKCE）
- refresh_token: 刷新 access_token（眼镜端滚动续期 / MCP 不滚动）

遵循 OAuth 2.1 Token 端点规范（RFC 6749 §5）。
"""

import jwt
from fastapi import APIRouter, Depends, Form, Header
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
from app.modules.mcp_oauth.repositories import McpAuthorizationRepository
from app.modules.mcp_oauth.service import (
    GRANT_AUTHORIZATION_CODE,
    MomentOAuthService,
)

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
        authorizations=McpAuthorizationRepository(session),
    )


def _make_mcp_oauth_service(
    settings: Settings = Depends(get_settings),
    jwt_issuer: JwtIssuer = Depends(_get_jwt_issuer),
    session: AsyncSession = Depends(get_db_session),
) -> MomentOAuthService:
    return MomentOAuthService(settings=settings, jwt_issuer=jwt_issuer, session=session)


class TokenResponse(BaseModel):
    """OAuth 2.1 Token 端点响应（RFC 6749 §5.1）。"""

    binding_id: str | None = Field(default=None, alias="binding_id")
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
    code: str | None = Form(default=None),
    code_verifier: str | None = Form(default=None),
    redirect_uri: str | None = Form(default=None),
    client_id: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
    service: DeviceBindingService = Depends(_make_service),
    mcp_oauth: MomentOAuthService = Depends(_make_mcp_oauth_service),
) -> TokenResponse:
    # 兼容 HTTP Basic 客户端认证（client_id[:client_secret]；ChatGPT/Claude 常用）
    if not client_id and authorization and authorization.lower().startswith("basic "):
        import base64

        try:
            decoded = base64.b64decode(authorization.removeprefix("Basic ").strip()).decode("utf-8")
            client_id = decoded.split(":", 1)[0] or None
        except Exception:
            client_id = None
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

    if grant_type == GRANT_AUTHORIZATION_CODE:
        # MCP OAuth：授权码 + PKCE 换我方 RS256 token
        if not code or not code_verifier or not client_id:
            raise ApplicationError(
                code="INVALID_REQUEST",
                message="code / code_verifier / client_id 是必需的。",
                status_code=400,
            )
        result = await mcp_oauth.exchange_auth_code(
            client_id=client_id,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )
        return TokenResponse(
            binding_id=result.binding_id,
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
        # 按 refresh token 的 grant claim 区分：MCP OAuth vs 眼镜端
        if _is_mcp_refresh_token(refresh_token):
            result = await mcp_oauth.refresh_mcp_token(refresh_token)
            return TokenResponse(
                binding_id=result.binding_id,
                access_token=result.access_token,
                refresh_token=result.refresh_token,
                token_type=result.token_type,
                expires_in=result.expires_in,
                scope=result.scope,
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


def _is_mcp_refresh_token(refresh_token: str) -> bool:
    """无验签读取 refresh_token 的 grant claim，路由到 MCP 刷新流程。"""
    try:
        unverified = jwt.decode(refresh_token, options={"verify_signature": False})
        return unverified.get("grant") == "authorization_code"
    except Exception:
        return False
