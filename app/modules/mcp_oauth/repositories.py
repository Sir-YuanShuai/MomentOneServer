"""MCP OAuth 存储访问层（mcp_oauth_clients / mcp_oauth_codes）。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import McpAuthorization, McpOAuthClient, McpOAuthCode
from app.modules.mcp.scope import CLIENT_TYPE_MCP


class McpClientRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        client_id: str,
        client_name: str | None,
        redirect_uris: list[str],
        scope: str,
        grant_types: list[str],
        token_endpoint_auth_method: str = "none",
    ) -> McpOAuthClient:
        orm = McpOAuthClient(
            id=uuid4(),
            client_id=client_id,
            client_name=client_name,
            redirect_uris=redirect_uris,
            scope=scope,
            grant_types=grant_types,
            token_endpoint_auth_method=token_endpoint_auth_method,
            status="active",
        )
        self._session.add(orm)
        await self._session.flush()
        return orm

    async def get_by_client_id(self, client_id: str) -> McpOAuthClient | None:
        stmt = select(McpOAuthClient).where(McpOAuthClient.client_id == client_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()


class McpAuthCodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        code: str,
        kind: str,
        client_id: str,
        redirect_uri: str | None,
        scope: str | None,
        state: str | None,
        code_challenge: str | None,
        casdoor_code_verifier: str | None,
        resource: str | None,
        user_id: UUID | None,
        expires_at: datetime,
    ) -> McpOAuthCode:
        orm = McpOAuthCode(
            id=uuid4(),
            code=code,
            kind=kind,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            state=state,
            code_challenge=code_challenge,
            casdoor_code_verifier=casdoor_code_verifier,
            resource=resource,
            user_id=user_id,
            status="pending",
            expires_at=expires_at,
        )
        self._session.add(orm)
        await self._session.flush()
        return orm

    async def get_by_code(self, code: str) -> McpOAuthCode | None:
        stmt = select(McpOAuthCode).where(McpOAuthCode.code == code)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_consumed(self, *, code_id: UUID) -> None:
        stmt = select(McpOAuthCode).where(McpOAuthCode.id == code_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is not None:
            orm.status = "consumed"
            await self._session.flush()


__all__ = ["McpAuthCodeRepository", "McpClientRepository"]


class McpAuthorizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        user_id: UUID,
        client_id: str,
        client_name: str | None,
        scope: str,
        client_type: str = CLIENT_TYPE_MCP,
    ) -> McpAuthorization:
        """授权完成时创建/更新授权关系（统一授权记录，覆盖 MCP 客户端与眼镜设备）。

        - 无记录（首次授权/绑定）→ 用本次 scope 创建
        - 已有 active 记录 → **保留用户已配置的 scope**（Web 端调整过的不被
          重连/重绑覆盖——ChatGPT 重连只请求 moments.read，不能把用户
          已授予的 moments.write 重置掉；眼镜重绑同理）
        - 已有 revoked 记录 → 重新授权，用本次 scope
        """
        stmt = select(McpAuthorization).where(
            McpAuthorization.user_id == user_id,
            McpAuthorization.client_id == client_id,
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        now = datetime.now(UTC)
        if orm is None:
            orm = McpAuthorization(
                id=uuid4(),
                user_id=user_id,
                client_id=client_id,
                client_name=client_name,
                client_type=client_type,
                scope=scope,
                status="active",
                last_active_at=now,
                created_at=now,
                updated_at=now,
            )
            self._session.add(orm)
        elif orm.status == "active":
            # 保留用户配置的 scope（Web 端可能已调整），仅刷新活跃信息
            orm.client_name = client_name or orm.client_name
            orm.client_type = client_type
            orm.last_active_at = now
            orm.updated_at = now
        else:
            # revoked → 重新授权
            orm.scope = scope
            orm.client_name = client_name or orm.client_name
            orm.client_type = client_type
            orm.status = "active"
            orm.revoked_at = None
            orm.last_active_at = now
            orm.updated_at = now
        await self._session.flush()
        return orm

    async def get_by_user_and_client(
        self, user_id: UUID, client_id: str
    ) -> McpAuthorization | None:
        stmt = select(McpAuthorization).where(
            McpAuthorization.user_id == user_id,
            McpAuthorization.client_id == client_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_user(self, user_id: UUID) -> list[McpAuthorization]:
        stmt = (
            select(McpAuthorization)
            .where(McpAuthorization.user_id == user_id)
            .order_by(McpAuthorization.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def touch_active(self, *, user_id: UUID, client_id: str) -> None:
        """token 验证时更新 last_active_at（active 授权）。"""
        stmt = select(McpAuthorization).where(
            McpAuthorization.user_id == user_id,
            McpAuthorization.client_id == client_id,
            McpAuthorization.status == "active",
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is not None:
            orm.last_active_at = datetime.now(UTC)
            orm.updated_at = datetime.now(UTC)
            await self._session.flush()

    async def update_scope_by_client(
        self, *, user_id: UUID, client_id: str, scope: str
    ) -> McpAuthorization | None:
        """按 client_id 更新 scope（眼镜设备改权限时设备端 API 调用）。"""
        stmt = select(McpAuthorization).where(
            McpAuthorization.user_id == user_id,
            McpAuthorization.client_id == client_id,
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            return None
        orm.scope = scope
        orm.updated_at = datetime.now(UTC)
        orm.revision = getattr(orm, "revision", 0) + 1
        await self._session.flush()
        return orm

    async def update_scope(
        self, *, authorization_id: UUID, user_id: UUID, scope: str
    ) -> McpAuthorization | None:
        stmt = select(McpAuthorization).where(
            McpAuthorization.id == authorization_id,
            McpAuthorization.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            return None
        orm.scope = scope
        orm.updated_at = datetime.now(UTC)
        orm.revision = getattr(orm, "revision", 0) + 1
        await self._session.flush()
        return orm

    async def revoke_by_client(self, *, user_id: UUID, client_id: str) -> McpAuthorization | None:
        """按 client_id 撤销（眼镜设备解绑时同步撤销授权记录）。"""
        stmt = select(McpAuthorization).where(
            McpAuthorization.user_id == user_id,
            McpAuthorization.client_id == client_id,
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            return None
        orm.status = "revoked"
        orm.revoked_at = datetime.now(UTC)
        orm.updated_at = datetime.now(UTC)
        orm.revision = getattr(orm, "revision", 0) + 1
        await self._session.flush()
        return orm

    async def revoke(self, *, authorization_id: UUID, user_id: UUID) -> McpAuthorization | None:
        stmt = select(McpAuthorization).where(
            McpAuthorization.id == authorization_id,
            McpAuthorization.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            return None
        orm.status = "revoked"
        orm.revoked_at = datetime.now(UTC)
        orm.updated_at = datetime.now(UTC)
        orm.revision = getattr(orm, "revision", 0) + 1
        await self._session.flush()
        return orm

    async def delete_revoked(self, *, authorization_id: UUID, user_id: UUID) -> bool:
        result = await self._session.execute(
            delete(McpAuthorization).where(
                McpAuthorization.id == authorization_id,
                McpAuthorization.user_id == user_id,
                McpAuthorization.status == "revoked",
            )
        )
        await self._session.flush()
        return bool(result.rowcount) if isinstance(result, CursorResult) else False


__all__ = [
    "McpAuthorizationRepository",
    "McpAuthCodeRepository",
    "McpClientRepository",
]
