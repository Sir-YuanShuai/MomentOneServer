from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.api.error_handlers import register_error_handlers
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.request_context import request_id_context
from app.infrastructure.database.session import init_database
from app.modules.mcp.endpoint import McpComponent

logger = structlog.get_logger()


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        token = request_id_context.set(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_context.reset(token)


class McpRequestLogMiddleware(BaseHTTPMiddleware):
    """临时诊断：记录每个 POST /mcp 请求的 JSON-RPC method（定位对话流调用链）。使用后移除。"""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.method == "POST" and request.url.path == "/mcp":
            try:
                raw = await request.body()
                import json as _json

                payload = _json.loads(raw)
                messages = payload if isinstance(payload, list) else [payload]
                methods = [m.get("method") for m in messages if isinstance(m, dict)]
                await logger.ainfo(
                    "mcp_request_methods",
                    methods=methods,
                    client=request.headers.get("user-agent", "")[:80],
                )
            except Exception:
                await logger.ainfo("mcp_request_unparseable")
        return await call_next(request)


def create_application(
    settings: Settings | None = None,
    mcp_component: McpComponent | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    mcp_component = mcp_component or McpComponent(resolved_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        await logger.ainfo("application_started", environment=resolved_settings.env)
        db = init_database(resolved_settings)
        async with mcp_component.run():
            yield
        await db.dispose()
        await logger.ainfo("application_stopped")

    app = FastAPI(
        title="Moment One API",
        version="0.1.0",
        description="Moment One cloud backend",
        debug=resolved_settings.debug,
        lifespan=lifespan,
    )

    app.add_middleware(McpRequestLogMiddleware)
    app.add_middleware(RequestContextMiddleware)

    if resolved_settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_settings.allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        )

    register_error_handlers(app)
    app.include_router(api_router)
    # MCP Streamable HTTP 端点（认证中间件由 SDK streamable_http_app 装配）
    app.mount("/", mcp_component.asgi_app)
    return app
