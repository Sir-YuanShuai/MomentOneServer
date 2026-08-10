"""MCP OAuth 路由：DCR + authorize 代理 + Casdoor 回调。

- POST /oauth/register   — RFC 7591 动态客户端注册
- GET  /oauth/authorize  — 校验客户端 → 302 跳转 Casdoor 登录
- GET  /oauth/callback   — Casdoor 回调 → 签发我方授权码 → 302 回客户端
- POST /oauth/token      — 授权码换 token / refresh（与眼镜端 QR Binding 共用端点）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.infrastructure.database.session import get_db_session
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.accounts.service import AccountCenterService
from app.modules.mcp_oauth.service import MomentOAuthService
from app.modules.quotas.repository import QuotaRepository

router = APIRouter(prefix="/oauth", tags=["mcp-oauth"])


def _get_jwt_issuer(settings: Settings = Depends(get_settings)) -> JwtIssuer:
    return JwtIssuer(settings)


def get_quota_repository(
    session: AsyncSession = Depends(get_db_session),
) -> QuotaRepository | None:
    return QuotaRepository(session)


def _make_account_center_service(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> AccountCenterService:
    return AccountCenterService(session, settings)


def _make_service(
    settings: Settings = Depends(get_settings),
    jwt_issuer: JwtIssuer = Depends(_get_jwt_issuer),
    session: AsyncSession = Depends(get_db_session),
    quotas: QuotaRepository | None = Depends(get_quota_repository),
) -> MomentOAuthService:
    return MomentOAuthService(
        settings=settings, jwt_issuer=jwt_issuer, session=session, quotas=quotas
    )


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
) -> RedirectResponse:
    """授权端点：校验客户端 + PKCE → 302 跳转 Casdoor 登录页（RFC 6749 §4.1.1）。

    MCP Host（ChatGPT / Claude）严格遵循标准：authorize 必须返回 302，
    不能返回 JSON（否则 Host 无法继续流程）。
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
        resource=resource,
    )
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
async def casdoor_callback(
    service: MomentOAuthService = Depends(_make_service),
    account_center: AccountCenterService = Depends(_make_account_center_service),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """Casdoor 登录回调：换 token → 识别用户 → 签发我方授权码 → 302 回客户端（RFC 6749 §4.1.2）。"""
    if state and await account_center.has_link_state(state):
        url = await account_center.complete_link_session(state=state, code=code, error=error)
        return RedirectResponse(url, status_code=302)
    if error:
        url = await service.handle_casdoor_callback(code=code or "", state=state or "", error=error)
        return RedirectResponse(url, status_code=302)
    if not code or not state:
        raise ApplicationError(
            code="INVALID_REQUEST",
            message="回调缺少 code 或 state。",
            status_code=400,
        )
    url = await service.handle_casdoor_callback(code=code, state=state)
    return RedirectResponse(url, status_code=302)


@router.get("/link-return", include_in_schema=False)
async def account_link_return(
    account_center: AccountCenterService = Depends(_make_account_center_service),
    state: str | None = None,
) -> RedirectResponse:
    url = await account_center.complete_provider_link_session(state=state)
    return RedirectResponse(url, status_code=302)


__all__ = ["router"]
