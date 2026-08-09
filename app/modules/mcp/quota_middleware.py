from typing import Any
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.types import ListToolsResult

from app.modules.mcp.deps import McpToolEnv


class McpToolVisibilityMiddleware:
    """对 tools/list 应用 Scope + Entitlement + Quota 可见性过滤。"""

    def __init__(self, env: McpToolEnv) -> None:
        self._env = env

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        result = await call_next(ctx)
        if ctx.method != "tools/list" or not isinstance(result, ListToolsResult):
            return result
        token = get_access_token()
        if token is None or not token.subject:
            result.tools = []
            return result
        try:
            user_id = UUID(token.subject)
        except (TypeError, ValueError):
            result.tools = []
            return result
        visible = await self._env.visible_tool_names(user_id, tuple(token.scopes or []))
        result.tools = [tool for tool in result.tools if tool.name in visible]
        return result
