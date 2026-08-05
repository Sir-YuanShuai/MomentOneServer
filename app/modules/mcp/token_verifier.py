"""MCP Server 的 Bearer Token 验证器（SDK `TokenVerifier` 协议实现）。

双形态验证（C5/C7 验收点）：
- **MCP OAuth token**（Authorization Code + PKCE 代理签发，Server 自签 RS256，
  `grant=authorization_code`）
- **眼镜端 QR Binding token**（现有 `POST /oauth/token` 签发，Server 自签 RS256，带 `binding_id`）
- **Casdoor OIDC token**（Web 端同一身份体系，JWKS 验签 + userinfo 同步）

统一返回 SDK 的 `AccessToken`（subject=本地 user_id），MCP 工具层通过
`get_access_token()` 读取，不关心 token 获取方式。
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

import jwt
from mcp.server.auth.provider import AccessToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.infrastructure.database.models import DeviceBinding as DeviceBindingORM
from app.infrastructure.database.repositories.user_repository import resolve_user_id
from app.infrastructure.identity.casdoor import CasdoorTokenVerifier
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.devices.domain import BindingStatus
from app.modules.mcp.scope import parse_scopes
from app.modules.mcp_oauth.repositories import McpAuthorizationRepository

# Casdoor OIDC token 的 scope 是 OIDC scope（openid/profile/email），
# 与 Moment One 的 moments.* scope 不同；Casdoor token 不携带 moments.* 时，
# 默认按最窄授权处理（MCP 工具层再用 moments.read 兜底见 tools.py 说明）。
CASDOOR_VERIFIED_SCOPES: tuple[str, ...] = ()


def _peek_issuer(token: str) -> str | None:
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
        return unverified.get("iss")
    except jwt.PyJWTError:
        return None


class SessionFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]: ...


class MomentTokenVerifier:
    """mcp SDK `TokenVerifier`：把 Bearer token 映射为本地用户身份。"""

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: SessionFactory | None = None,
        casdoor_verifier: CasdoorTokenVerifier | None = None,
    ) -> None:
        self._settings = settings
        self._jwt_issuer = JwtIssuer(settings)
        self._casdoor_verifier = casdoor_verifier or CasdoorTokenVerifier(settings)
        # 会话工厂：`async with session_factory() as session`；None 时用全局 DB
        self._session_factory = session_factory

    def _make_session(self) -> AbstractAsyncContextManager[AsyncSession]:
        if self._session_factory is not None:
            return self._session_factory()
        from app.infrastructure.database.session import get_database

        db = get_database()
        return db.session_factory()

    async def verify_token(self, token: str) -> AccessToken | None:
        """验证 Bearer token，返回 SDK AccessToken（None = 无效 → 401）。"""
        issuer = _peek_issuer(token)

        if issuer == self._jwt_issuer.issuer:
            return await self._verify_server_issued(token)

        # Casdoor OIDC 路径
        try:
            async with self._make_session() as session:
                user_id = await resolve_user_id(session, self._casdoor_verifier, token)
        except Exception:
            return None

        return AccessToken(
            token=token,
            client_id="casdoor-web",
            scopes=[*CASDOOR_VERIFIED_SCOPES],
            subject=str(user_id),
            claims={"method": "casdoor"},
        )

    async def _verify_server_issued(self, token: str) -> AccessToken | None:
        try:
            payload = self._jwt_issuer.verify_access_token(token)
        except Exception:
            return None

        try:
            user_id = UUID(payload["sub"])
        except (KeyError, ValueError, TypeError):
            return None

        scope = parse_scopes(payload.get("scope"))
        grant = payload.get("grant")
        client_id = payload.get("client_id")

        if grant == "authorization_code":
            # MCP OAuth token：校验授权关系仍 active（Web 端撤销后立即失效），
            # 且 scope 以授权记录（mcp_authorizations）为准——Web 端调整 scope
            # 后**无需重连/刷新，下一次调用即实时生效**（token 里的 scope 仅作签发快照）
            async with self._make_session() as session:
                repo = McpAuthorizationRepository(session)
                authorization = await repo.get_by_user_and_client(user_id, client_id or "")
                if authorization is None or authorization.status != "active":
                    return None
                effective_scope = parse_scopes(authorization.scope)
                await repo.touch_active(user_id=user_id, client_id=client_id or "")
            return AccessToken(
                token=token,
                client_id=client_id or "mcp-oauth",
                scopes=[*effective_scope],
                subject=str(user_id),
                claims={
                    "method": "mcp",
                    "grant": "authorization_code",
                    "client_id": client_id,
                    "exp": payload.get("exp"),
                },
            )

        # 眼镜端 QR Binding token：校验 binding 仍 active
        binding_id = payload.get("binding_id")
        if binding_id is None:
            return None
        try:
            binding_uuid = UUID(binding_id)
        except (ValueError, TypeError):
            return None

        async with self._make_session() as session:
            stmt = select(DeviceBindingORM).where(DeviceBindingORM.id == binding_uuid)
            result = await session.execute(stmt)
            binding = result.scalar_one_or_none()
        if binding is None or binding.status != BindingStatus.ACTIVE.value:
            return None

        return AccessToken(
            token=token,
            client_id=f"glasses:{payload.get('device_id', '')}",
            scopes=[*scope],
            subject=str(user_id),
            claims={
                "method": "glasses",
                "binding_id": binding_id,
                "device_id": payload.get("device_id"),
            },
        )


__all__ = ["MomentTokenVerifier"]
