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
from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.infrastructure.database.models import DeviceBinding as DeviceBindingORM
from app.infrastructure.database.repositories.user_repository import resolve_user_id
from app.infrastructure.database.session import get_db_session
from app.infrastructure.identity.casdoor import CasdoorTokenVerifier
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.devices.domain import BindingStatus


@dataclass(frozen=True, slots=True)
class AuthContext:
    """鉴权上下文，携带 user_id 和来源信息（用于 provenance 推断）。"""

    user_id: UUID
    method: str  # "casdoor" | "glasses"
    device_id: str | None = None
    binding_id: UUID | None = None


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


async def _verify_glasses_token(
    token: str,
    jwt_issuer: JwtIssuer,
    session: AsyncSession,
) -> AuthContext:
    """眼镜端 access_token 鉴权路径。

    验签 → 取 sub(本地 user_id) + binding_id + device_id → 校验 binding active → 返回 AuthContext。
    """
    payload = jwt_issuer.verify_access_token(token)
    user_id = UUID(payload["sub"])
    binding_id = UUID(payload["binding_id"])
    device_id = payload.get("device_id")

    # 校验 binding 仍 active，收紧撤销后 access_token 的可用窗口
    stmt = select(DeviceBindingORM).where(DeviceBindingORM.id == binding_id)
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
        binding_id=binding_id,
    )


async def get_auth_context(
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
        return await _verify_glasses_token(token, jwt_issuer, session)

    # Casdoor OIDC 路径
    user_id = await resolve_user_id(session, casdoor_verifier, token)
    return AuthContext(user_id=user_id, method="casdoor")


async def get_authenticated_user_id(
    ctx: AuthContext = Depends(get_auth_context),
) -> UUID:
    """便捷依赖：仅返回 user_id，用于不需要 provenance 的路由。"""
    return ctx.user_id


__all__ = ["AuthContext", "get_auth_context", "get_authenticated_user_id"]
