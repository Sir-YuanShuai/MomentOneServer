"""MCP OAuth 路由：DCR + authorize 代理 + Casdoor 回调。

- POST /oauth/register   — RFC 7591 动态客户端注册
- GET  /oauth/authorize  — 校验客户端 → 302 跳转 Casdoor 登录
- GET  /oauth/callback   — Casdoor 回调 → 签发我方授权码 → 302 回客户端
- POST /oauth/token      — 授权码换 token / refresh（与眼镜端 QR Binding 共用端点）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.infrastructure.database.session import get_db_session
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.mcp_oauth.service import MomentOAuthService

router = APIRouter(prefix="/oauth", tags=["mcp-oauth"])


def _get_jwt_issuer(settings: Settings = Depends(get_settings)) -> JwtIssuer:
    return JwtIssuer(settings)


def _make_service(
    settings: Settings = Depends(get_settings),
    jwt_issuer: JwtIssuer = Depends(_get_jwt_issuer),
    session: AsyncSession = Depends(get_db_session),
) -> MomentOAuthService:
    return MomentOAuthService(settings=settings, jwt_issuer=jwt_issuer, session=session)


class RegisterResponse(BaseModel):
    client_id: str
    client_id_issued_at: int | None = None
    client_name: str | None = None
    redirect_uris: list[str] = Field(default_factory=list)
    scope: str | None = None
    grant_types: list[str] = Field(default_factory=list)
    token_endpoint_auth_method: str = "none"
    registration_client_uri: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_in: int
    scope: str


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register_client(
    request: Request,
    service: MomentOAuthService = Depends(_make_service),
) -> RegisterResponse:
    """RFC 7591 动态客户端注册（MCP Host 首次连接时调用）。"""
    try:
        body = await request.json()
    except Exception as exc:
        raise ApplicationError(
            code="INVALID_CLIENT_METADATA",
            message="注册请求体不是合法 JSON。",
            status_code=400,
        ) from exc
    result = await service.register_client(body)
    return RegisterResponse(
        client_id=result["client_id"],
        client_id_issued_at=result.get("client_id_issued_at"),
        client_name=result.get("client_name"),
        redirect_uris=result.get("redirect_uris", []),
        scope=result.get("scope"),
        grant_types=result.get("grant_types", []),
        token_endpoint_auth_method="none",
        registration_client_uri=result.get("registration_client_uri"),
    )


@router.get("/authorize")
async def authorize(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    service: MomentOAuthService = Depends(_make_service),
    scope: str | None = None,
    state: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
    resource: str | None = None,  # RFC 8707，忽略但接受（MCP Host 会带）
) -> dict:
    """授权端点：校验客户端 + PKCE → 302 跳转 Casdoor 登录页。

    返回 302 跳转（FastAPI 路由层通过 RedirectResponse 处理），
    这里返回 {redirect_to} 由路由包装。
    """
    if response_type != "code":
        raise ApplicationError(
            code="UNSUPPORTED_RESPONSE_TYPE",
            message="仅支持 response_type=code。",
            status_code=400,
        )
    url = await service.start_authorize(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )
    return {"redirect_to": url}


@router.get("/callback")
async def casdoor_callback(
    service: MomentOAuthService = Depends(_make_service),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> dict:
    """Casdoor 登录回调：换 token → 识别用户 → 签发我方授权码 → 302 回客户端。"""
    if error:
        raise ApplicationError(
            code="OAUTH_DENIED",
            message=f"用户在 Casdoor 拒绝了授权：{error}",
            status_code=400,
            details={"error": error, "errorDescription": error_description},
        )
    if not code or not state:
        raise ApplicationError(
            code="INVALID_REQUEST",
            message="回调缺少 code 或 state。",
            status_code=400,
        )
    url = await service.handle_casdoor_callback(code=code, state=state)
    return {"redirect_to": url}


__all__ = ["router"]
