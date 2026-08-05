"""构建 Moment One MCP Server（mcp 官方 Python SDK）。

- Streamable HTTP（`POST /mcp`，挂载见 application.py）
- Bearer 鉴权由 `BearerAuthBackend(MomentTokenVerifier)` + `RequireAuthMiddleware` 处理
- 工具：bookkeeping_create / bookkeeping_list / bookkeeping_summary / moments_get
- MCP Apps：`ui://moment-one/bookkeeping`（记账列表 + 简单统计，app-bridge 调用）
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from mcp.server.apps import Apps
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from app.modules.mcp import tools
from app.modules.mcp.deps import McpToolEnv

logger = logging.getLogger(__name__)

BOOKKEEPING_UI_URI = "ui://moment-one/bookkeeping"

# Apps HTML 缺失时的降级资源（保证 tools/list 与工具调用不因 UI 文件缺失而失败）
_FALLBACK_APPS_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>Moment One 记账</title></head>
<body>
  <div id="root" style="font-family: system-ui; padding: 16px;">
    <p>Moment One 记账 App 未配置（mcp_apps_html_path 指向的 HTML 文件缺失）。</p>
    <p>请通过 MCP 工具调用获取数据。</p>
  </div>
</body>
</html>"""

# 工具描述（供模型理解；错误码与 docs/contracts/MCP_SERVER.md §7 对齐）
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "bookkeeping_create": (
        "记录一笔账（bookkeeping）。需要 moments.write 权限。"
        "amount 金额、flow 流向（expense/income）、occurredAt 发生时间（ISO-8601）为必填；"
        "category 分类（如餐饮、交通）、merchant 商家、ledger 账本、account 账户可选。"
        "支持 idempotencyKey 幂等重试。非法 payload 返回 INVALID_ARGUMENTS。"
    ),
    "bookkeeping_list": (
        "按时间倒序列出记账记录。limit 不超过 20，支持 cursor 分页与 from/to/category/ledger 过滤。"
    ),
    "bookkeeping_summary": (
        "记账统计（服务端聚合）：周期内收支合计 + 分类小计，口径与 Web 记账板块一致。"
        "period 为 month/quarter/year，可指定 year/month（month 为月份或季度号）。"
    ),
    "moments_get": "按 momentId 查询单条完整 Moment（含 type/payload/provenance）。",
}


def build_mcp_server(
    *,
    env: McpToolEnv,
    apps_html: str | None = None,
    token_verifier: TokenVerifier | None = None,
    auth: AuthSettings | None = None,
) -> MCPServer:
    apps = Apps()
    # 顺序约束：Apps 扩展在 MCPServer 构造时应用（工具/资源注册），
    # 因此绑定 ui:// 的工具和 HTML 资源必须先注册进 apps 实例。
    _register_bookkeeping_list(apps, env)
    _register_bookkeeping_summary(apps, env)
    apps.add_html_resource(
        BOOKKEEPING_UI_URI,
        apps_html or _FALLBACK_APPS_HTML,
        name="Moment One 记账",
        title="记账",
        description="记账记录列表 + 收支统计（ui://moment-one/bookkeeping）",
    )
    if not apps_html:
        logger.warning(
            "mcp_apps_html_path 未配置或文件缺失：ui://moment-one/bookkeeping 使用降级占位资源"
        )

    server = MCPServer(
        name="moment-one-mcp",
        title="Moment One MCP Server",
        description="Moment One 个人生活记忆系统：记账读写工具（第一版）。",
        version="0.1.0",
        extensions=[apps],
        token_verifier=token_verifier,
        auth=auth,
    )

    # 非 Apps 绑定工具在 server 构造后注册
    _register_bookkeeping_create(server, env)
    _register_moments_get(server, env)

    return server


# ---------------------------------------------------------------------------
# bookkeeping_create（写，moments.write）
# ---------------------------------------------------------------------------


def _register_bookkeeping_create(server: MCPServer, env: McpToolEnv) -> None:
    @server.tool(
        name="bookkeeping_create",
        description=_TOOL_DESCRIPTIONS["bookkeeping_create"],
        title="记一笔账",
    )
    async def bookkeeping_create(  # pyright: ignore[reportUnusedFunction]
        amount: Annotated[float, Field(ge=0, le=9999999, description="金额（0 ~ 9999999）")],
        flow: Annotated[
            Literal["expense", "income"], Field(description="流向：expense=支出，income=收入")
        ],
        occurredAt: Annotated[
            str, Field(description="发生时间（ISO-8601，如 2026-08-05T12:00:00+08:00）")
        ],
        account: Annotated[
            str | None,
            Field(default=None, max_length=30, description="收支账户（如：微信、支付宝）"),
        ] = None,
        category: Annotated[
            str | None, Field(default=None, max_length=30, description="分类（如：餐饮、交通）")
        ] = None,
        merchant: Annotated[
            str | None, Field(default=None, max_length=50, description="商家（可选）")
        ] = None,
        ledger: Annotated[
            str | None, Field(default=None, max_length=30, description="账本（如：日常、旅行）")
        ] = None,
        method: Annotated[
            str | None, Field(default=None, max_length=20, description="支付方式（可选）")
        ] = None,
        countInFlow: Annotated[
            bool | None, Field(default=None, description="是否计入收支统计（默认 true）")
        ] = None,
        countInBudget: Annotated[
            bool | None, Field(default=None, description="是否计入预算（默认 true）")
        ] = None,
        idempotencyKey: Annotated[
            str | None, Field(default=None, description="幂等键（可选，重试安全）")
        ] = None,
        title: Annotated[
            str | None,
            Field(default=None, max_length=20, description="标题（可选，默认取分类/商家）"),
        ] = None,
    ) -> object:
        return await env.call(
            lambda ctx: tools.bookkeeping_create(
                ctx,
                amount=amount,
                flow=flow,
                occurred_at=occurredAt,
                account=account,
                category=category,
                merchant=merchant,
                ledger=ledger,
                method=method,
                count_in_flow=countInFlow,
                count_in_budget=countInBudget,
                idempotency_key=idempotencyKey,
                title=title,
            )
        )


# ---------------------------------------------------------------------------
# bookkeeping_list（读，moments.read，绑定 Apps UI）
# ---------------------------------------------------------------------------


def _register_bookkeeping_list(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=BOOKKEEPING_UI_URI,
        name="bookkeeping_list",
        description=_TOOL_DESCRIPTIONS["bookkeeping_list"],
        title="记账记录列表",
    )
    async def bookkeeping_list(  # pyright: ignore[reportUnusedFunction]
        limit: Annotated[int, Field(default=20, ge=1, le=20, description="每页数量（≤20）")] = 20,
        cursor: Annotated[
            str | None, Field(default=None, description="不透明分页游标（上页 nextCursor）")
        ] = None,
        from_: Annotated[
            str | None, Field(default=None, description="开始时间（ISO-8601）")
        ] = None,
        to: Annotated[str | None, Field(default=None, description="结束时间（ISO-8601）")] = None,
        category: Annotated[str | None, Field(default=None, description="按分类过滤")] = None,
        ledger: Annotated[
            str | None, Field(default=None, description="按账本过滤（payload.ledger）")
        ] = None,
    ) -> object:
        return await env.call(
            lambda ctx: tools.bookkeeping_list(
                ctx,
                limit=limit,
                cursor=cursor,
                from_=from_,
                to=to,
                category=category,
                ledger=ledger,
            )
        )


# ---------------------------------------------------------------------------
# bookkeeping_summary（读，moments.read，绑定 Apps UI）
# ---------------------------------------------------------------------------


def _register_bookkeeping_summary(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=BOOKKEEPING_UI_URI,
        name="bookkeeping_summary",
        description=_TOOL_DESCRIPTIONS["bookkeeping_summary"],
        title="记账统计",
    )
    async def bookkeeping_summary(  # pyright: ignore[reportUnusedFunction]
        period: Annotated[Literal["month", "quarter", "year"], Field(description="统计周期")],
        year: Annotated[int | None, Field(default=None, description="年份（默认当前年）")] = None,
        month: Annotated[
            int | None,
            Field(default=None, description="月份（period=month 时 1~12；period=quarter 时 1~4）"),
        ] = None,
        ledger: Annotated[str | None, Field(default=None, description="按账本过滤")] = None,
        category: Annotated[
            str | None, Field(default=None, description="按分类过滤（分类小计）")
        ] = None,
    ) -> object:
        return await env.call(
            lambda ctx: tools.bookkeeping_summary(
                ctx,
                period=period,
                year=year,
                month=month,
                ledger=ledger,
                category=category,
            )
        )


# ---------------------------------------------------------------------------
# moments_get（读，moments.read）
# ---------------------------------------------------------------------------


def _register_moments_get(server: MCPServer, env: McpToolEnv) -> None:
    @server.tool(
        name="moments_get",
        description=_TOOL_DESCRIPTIONS["moments_get"],
        title="查询单条 Moment",
    )
    async def moments_get(  # pyright: ignore[reportUnusedFunction]
        momentId: Annotated[str, Field(description="Moment ID（UUID）")],
    ) -> object:
        return await env.call(lambda ctx: tools.moments_get(ctx, moment_id=momentId))


__all__ = ["BOOKKEEPING_UI_URI", "build_mcp_server"]
