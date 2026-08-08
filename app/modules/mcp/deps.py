"""MCP 工具运行环境：身份提取 → 会话 → 执行 → 提交/错误映射。

SDK 通过 `BearerAuthBackend(MomentTokenVerifier)` 把 token 验证结果放到
contextvar，工具层用 `get_access_token()` 读取（SDK 的 AuthContextMiddleware 负责
contextvar 注入）。本模块把「身份 + DB 会话 + 错误映射」统一封装，
工具函数只关心业务逻辑（见 tools.py）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApplicationError
from app.modules.mcp.a2ui import A2UI_DISABLED, A2UISupport
from app.modules.mcp.tools import McpCallContext, err_result

logger = structlog.get_logger()

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class McpToolEnv:
    """一次 MCPServer 实例的工具执行环境。"""

    def __init__(
        self,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        # session_factory 返回 async context manager（async with 使用）
        self._session_factory: SessionFactory = session_factory or _default_session_factory

    async def call(
        self,
        fn: Callable[[McpCallContext], Awaitable[object]],
        *,
        a2ui_support: A2UISupport = A2UI_DISABLED,
    ) -> object:
        """执行一次工具调用：提取身份 → 开 session → 执行 → 提交。

        返回 CallToolResult（成功或 isError）。任何未捕获异常兜底为 INTERNAL_ERROR。
        """
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        if token is None:
            return err_result("AUTH_REQUIRED", "缺少认证上下文，请携带 Bearer Token 重试。")

        subject = token.subject
        try:
            user_id = UUID(subject) if subject else None
        except (ValueError, TypeError):
            user_id = None
        if user_id is None:
            return err_result("TOKEN_INVALID", "Token 未携带有效用户身份。")

        scopes = tuple(token.scopes or [])
        claims = token.claims or {}
        method = claims.get("method", "mcp")
        actor_id = claims.get("client_id") or claims.get("device_id") or token.client_id

        async with self._session_factory() as session:
            ctx = McpCallContext(
                user_id=user_id,
                scopes=scopes,
                method=method,
                actor_id=actor_id,
                request_id=str(uuid4()),
                session=session,
                a2ui=a2ui_support,
            )
            try:
                result = await fn(ctx)
                await session.commit()
                return result
            except ApplicationError as exc:
                await session.rollback()
                await logger.ainfo(
                    "mcp_tool_rejected",
                    code=exc.code,
                    user_id=str(user_id),
                    request_id=ctx.request_id,
                )
                return err_result(exc.code, exc.message, exc.details)
            except Exception:
                await session.rollback()
                await logger.aexception(
                    "mcp_tool_failed", user_id=str(user_id), request_id=ctx.request_id
                )
                return err_result("INTERNAL_ERROR", "服务器内部错误，请稍后重试。")


@asynccontextmanager
async def _default_session_factory() -> AsyncGenerator[AsyncSession]:
    from app.infrastructure.database.session import get_database

    async with get_database().session_factory() as session:
        yield session


__all__ = ["McpToolEnv"]
