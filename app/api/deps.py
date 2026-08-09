"""共享鉴权依赖：支持 Casdoor OIDC 和眼镜端 JWT 双通道。

路由通过 `get_authenticated_user_id` 依赖注入本地 user_id (UUID)。
内部按 token 的 iss 路由：
- iss == settings.jwt_issuer → 眼镜端 access_token（RS256，Server 自签自验）
- 其他 → Casdoor OIDC Access Token（JWKS 验签 + userinfo 同步）

眼镜端 token 的 sub 直接是本地 users.id，验签后还需校验 binding 仍 active，
防止已撤销绑定的 token 在 exp 前继续操作（refresh 立即失败，access 在 exp 前仍可用
是设计取舍，但业务 API 层加 binding 校验可收紧到"撤销即失效"）。
"""

from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.infrastructure.database.models import DeviceBinding as DeviceBindingORM
from app.infrastructure.database.repositories.user_repository import UserRepository, resolve_user_id
from app.infrastructure.database.session import get_db_session
from app.infrastructure.identity.casdoor import CasdoorTokenVerifier
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.devices.domain import BindingStatus


@dataclass(frozen=True, slots=True)
class AuthContext:
    """鉴权上下文，携带 user_id 和来源信息（用于 provenance 推断）。"""

    user_id: UUID
    method: str  # "casdoor" | "glasses" | "mcp"
    device_id: str | None = None
    binding_id: UUID | None = None
    client_id: str | None = None
    scope: tuple[str, ...] | None = None
    issued_at: int | None = None


def _get_casdoor_verifier(settings: Settings = Depends(get_settings)) -> CasdoorTokenVerifier:
    return CasdoorTokenVerifier(settings)


def _get_jwt_issuer(settings: Settings = Depends(get_settings)) -> JwtIssuer:
    return JwtIssuer(settings)


def _peek_issuer(token: str) -> str | None:
    """无验签地读取 JWT 的 iss claim，用于路由判断。"""
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
        return unverified.get("iss")
    except jwt.PyJWTError:
        return None


async def _verify_server_issued_token(
    token: str,
    jwt_issuer: JwtIssuer,
    session: AsyncSession,
) -> AuthContext:
    """Server 自签 RS256 token 鉴权路径（眼镜端 + MCP OAuth 双形态）。

    验签后按 claims 区分：
    - 带 binding_id → 眼镜端 QR Binding token（校验 binding 仍 active）
    - grant=authorization_code → MCP OAuth token（PKCE 代理签发，无绑定关系）
    """
    payload = jwt_issuer.verify_access_token(token)
    user_id = UUID(payload["sub"])
    user_repo = UserRepository(session)
    user = await user_repo.get(user_id)
    if user is None:
        raise ApplicationError(
            code="TOKEN_INVALID", message="token 对应的用户不存在。", status_code=401
        )
    UserRepository.ensure_active(user)
    await user_repo.touch_active(user)
    scope = tuple((payload.get("scope") or "").split())
    grant = payload.get("grant")

    if grant == "authorization_code":
        # MCP OAuth（Authorization Code + PKCE 代理）token
        return AuthContext(
            user_id=user_id,
            method="mcp",
            client_id=payload.get("client_id"),
            scope=scope,
            issued_at=payload.get("iat") if isinstance(payload.get("iat"), int) else None,
        )

    # 眼镜端 QR Binding token：必须带 binding_id
    binding_id = payload.get("binding_id")
    if binding_id is None:
        raise ApplicationError(
            code="TOKEN_INVALID",
            message="token 缺少 binding_id，无法识别授权来源。",
            status_code=401,
        )
    binding_uuid = UUID(binding_id)
    device_id = payload.get("device_id")

    # 校验 binding 仍 active，收紧撤销后 access_token 的可用窗口
    stmt = select(DeviceBindingORM).where(DeviceBindingORM.id == binding_uuid)
    result = await session.execute(stmt)
    binding = result.scalar_one_or_none()
    if binding is None or binding.status != BindingStatus.ACTIVE.value:
        raise ApplicationError(
            code="TOKEN_INVALID",
            message="设备绑定已失效，请重新扫码绑定。",
            status_code=401,
        )

    return AuthContext(
        user_id=user_id,
        method="glasses",
        device_id=device_id,
        binding_id=binding_uuid,
        scope=scope,
        issued_at=payload.get("iat") if isinstance(payload.get("iat"), int) else None,
    )


async def get_auth_context(
    request: Request,
    settings: Settings = Depends(get_settings),
    casdoor_verifier: CasdoorTokenVerifier = Depends(_get_casdoor_verifier),
    jwt_issuer: JwtIssuer = Depends(_get_jwt_issuer),
    session: AsyncSession = Depends(get_db_session),
    authorization: str | None = Header(default=None),
) -> AuthContext:
    """统一鉴权依赖：按 iss 路由到 Casdoor 或眼镜端 JWT 验证。

    Returns:
        AuthContext，包含 user_id 和来源信息。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise ApplicationError(
            code="AUTH_REQUIRED",
            message="请先登录。",
            status_code=401,
        )

    token = authorization.removeprefix("Bearer ").strip()
    issuer = _peek_issuer(token)

    if issuer == jwt_issuer.issuer:
        context = await _verify_server_issued_token(token, jwt_issuer, session)
        request.state.auth_user_id = context.user_id
        request.state.auth_method = context.method
        request.state.auth_client_id = context.client_id
        request.state.auth_device_id = context.device_id
        return context

    # Casdoor OIDC 路径
    principal = casdoor_verifier.verify(token)
    user_id = await resolve_user_id(session, casdoor_verifier, token)
    context = AuthContext(user_id=user_id, method="casdoor", issued_at=principal.issued_at)
    request.state.auth_user_id = context.user_id
    request.state.auth_method = context.method
    request.state.auth_client_id = None
    request.state.auth_device_id = None
    return context


async def get_authenticated_user_id(
    ctx: AuthContext = Depends(get_auth_context),
) -> UUID:
    """便捷依赖：仅返回 user_id，用于不需要 provenance 的路由。"""
    return ctx.user_id


__all__ = ["AuthContext", "get_auth_context", "get_authenticated_user_id"]
