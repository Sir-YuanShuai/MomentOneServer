"""MCP Streamable HTTP 端点装配。

使用 SDK 官方模式（examples/servers/simple-auth）：
`MCPServer(token_verifier=..., auth=AuthSettings(...)).streamable_http_app()`
SDK 自动接入：
- AuthenticationMiddleware(BearerAuthBackend(verifier)) → 验证 Bearer token
- AuthContextMiddleware → contextvar 注入（工具层 get_access_token() 读取）
- RequireAuthMiddleware → 401 + `WWW-Authenticate: Bearer resource_metadata=...`
- RFC 9728 path-aware Protected Resource Metadata（`/.well-known/oauth-protected-resource/mcp`）

我们不传 auth_server_provider：authorize/token/register 与发现端点（根路径 + /mcp
子路径）由主 FastAPI 应用提供（见 api/routes/mcp_oauth.py / mcp_discovery.py）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from app.core.config import Settings
from app.modules.mcp.deps import McpToolEnv
from app.modules.mcp.server import build_mcp_server
from app.modules.mcp.token_verifier import MomentTokenVerifier

logger = logging.getLogger(__name__)


# MCP Apps UI 构建产物默认位置（相对仓库根/容器工作目录 /app，双环境可用）
_DEFAULT_APPS_HTML_REL = "mcp_apps/bookkeeping/dist/bookkeeping.html"


def _load_apps_html(settings: Settings) -> str | None:
    """读取 MCP Apps UI 单文件产物（bookkeeping.html）。

    候选顺序：settings.mcp_apps_html_path → 默认相对路径（本地仓库根 / 容器 /app
    均适用），保证未配置环境变量时也能加载真实 UI（降级占位仅作为最后兜底）。
    """
    candidates = [settings.mcp_apps_html_path, _DEFAULT_APPS_HTML_REL]
    for path in candidates:
        if not path:
            continue
        p = Path(path)
        if p.is_file():
            return p.read_text(encoding="utf-8")
        if path == settings.mcp_apps_html_path:
            logger.warning("mcp_apps_html_path 指向的文件不存在：%s", path)
    logger.warning(
        "MCP Apps UI 未找到（%s），ui://moment-one/bookkeeping 使用降级占位资源",
        _DEFAULT_APPS_HTML_REL,
    )
    return None


class McpComponent:
    """MCP 端点组件：session manager + ASGI app + 工具环境。"""

    def __init__(
        self,
        settings: Settings,
        *,
        verifier: MomentTokenVerifier | None = None,
        env: McpToolEnv | None = None,
    ) -> None:
        self.settings = settings
        self.verifier = verifier or MomentTokenVerifier(settings)
        self.env = env or McpToolEnv()
        apps_html = _load_apps_html(settings)

        base = (settings.mcp_base_url or "http://127.0.0.1:8000").rstrip("/")
        # host 参数：SDK 用它决定是否启用默认 DNS rebinding 保护（仅 localhost 触发）。
        # 线上必须传真实域名（host=moment-one-api.yuanshuai.fun），否则带 token 的
        # 请求被 421 Invalid Host header 拒绝；本地默认 127.0.0.1 保护照常。
        host = urlparse(base).hostname or "127.0.0.1"
        self.server = build_mcp_server(
            env=self.env,
            apps_html=apps_html,
            token_verifier=self.verifier,
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(base),
                resource_server_url=AnyHttpUrl(f"{base}/mcp"),
                required_scopes=None,  # 工具级 scope 校验在 tools.py（moments.read / write）
            ),
        )

        self.asgi_app = self.server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            host=host,
        )

    @asynccontextmanager
    async def run(self) -> AsyncGenerator[None]:
        """在 FastAPI lifespan 中运行 session manager（挂载的 Starlette 子应用
        不跑自己的 lifespan，需要在这里手动进入）。"""
        async with self.server.session_manager.run():
            yield


def create_mcp_component(settings: Settings) -> McpComponent:
    return McpComponent(settings)


__all__ = ["McpComponent", "create_mcp_component"]
