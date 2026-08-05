"""MCP OAuth 存储访问层（mcp_oauth_clients / mcp_oauth_codes）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import McpOAuthClient, McpOAuthCode


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
