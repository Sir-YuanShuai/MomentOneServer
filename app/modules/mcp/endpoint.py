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
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from app.core.config import Settings
from app.infrastructure.storage.object_storage import ObjectStorageNotConfigured, get_object_storage
from app.modules.mcp.deps import McpToolEnv
from app.modules.mcp.server import build_mcp_server
from app.modules.mcp.token_verifier import MomentTokenVerifier

logger = logging.getLogger(__name__)


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
        if env is None:
            try:
                storage = get_object_storage(settings)
            except ObjectStorageNotConfigured:
                storage = None
            self.env = McpToolEnv(
                object_storage=storage,
                max_upload_bytes=settings.max_upload_bytes,
                upload_url_ttl_seconds=settings.s3_upload_url_ttl_seconds,
            )
        else:
            self.env = env
        base = (settings.mcp_base_url or "http://127.0.0.1:8000").rstrip("/")
        # host 参数：SDK 用它决定是否启用默认 DNS rebinding 保护（仅 localhost 触发）。
        # 线上必须传真实域名（host=moment-one-api.yuanshuai.fun），否则带 token 的
        # 请求被 421 Invalid Host header 拒绝；本地默认 127.0.0.1 保护照常。
        host = urlparse(base).hostname or "127.0.0.1"
        self.server = build_mcp_server(
            env=self.env,
            apps_asset_base_url=settings.mcp_apps_asset_base_url,
            apps_version=settings.mcp_apps_version,
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
