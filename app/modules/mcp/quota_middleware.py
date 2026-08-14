from typing import Any
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.types import ListToolsResult

from app.modules.mcp.deps import GLASSES_ONLY_TOOLS, McpToolEnv


class McpToolVisibilityMiddleware:
    """对 tools/list 应用 Scope + Entitlement + Quota 可见性过滤。"""

    def __init__(self, env: McpToolEnv) -> None:
        self._env = env

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        token = get_access_token() if ctx.method == "tools/list" else None
        request_user = getattr(ctx.request, "user", None)
        if token is None and isinstance(request_user, AuthenticatedUser):
            # Streamable HTTP may dispatch the MCP message in a child task where
            # the ASGI auth ContextVar is no longer present. The verified user is
            # retained on the request scope and is the authoritative fallback.
            token = request_user.access_token
        result = await call_next(ctx)
        if ctx.method != "tools/list" or not isinstance(result, (ListToolsResult, dict)):
            return result
        if token is None or not token.subject:
            if isinstance(result, dict):
                result["tools"] = []
            else:
                result.tools = []
            return result
        try:
            user_id = UUID(token.subject)
        except (TypeError, ValueError):
            if isinstance(result, dict):
                result["tools"] = []
            else:
                result.tools = []
            return result
        visible = await self._env.visible_tool_names(user_id, tuple(token.scopes or []))
        claims = token.claims or {}
        is_glasses = claims.get("method") == "glasses"
        if not is_glasses:
            visible = visible - GLASSES_ONLY_TOOLS
        if isinstance(result, dict):
            filtered_tools: list[Any] = [
                tool
                for tool in result.get("tools", [])
                if isinstance(tool, dict) and tool.get("name") in visible
            ]
            result["tools"] = filtered_tools
        else:
            result.tools = [tool for tool in result.tools if tool.name in visible]
            filtered_tools = result.tools
        if is_glasses:
            # 眼镜只消费 A2UI，不能收到 MCP Apps 的 ui:// 模板绑定。
            for tool in filtered_tools:
                meta = tool.get("_meta") if isinstance(tool, dict) else tool.meta
                if meta:
                    meta.pop("ui", None)
                    meta.pop("openai/outputTemplate", None)
        return result
