"""构建 Moment One MCP Server（mcp 官方 Python SDK）。

- Streamable HTTP（`POST /mcp`，挂载见 application.py）
- Bearer 鉴权由 `BearerAuthBackend(MomentTokenVerifier)` + `RequireAuthMiddleware` 处理
- 工具：记账 + 通用 Moment + 每日回顾 + 习惯目标/打卡
- MCP Apps：记账、记忆时间线、习惯追踪三套交互 UI
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.apps import Apps
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from app.modules.mcp import tools
from app.modules.mcp.a2ui import A2UIExtension, negotiate_a2ui
from app.modules.mcp.deps import McpToolEnv
from app.modules.mcp.quota_middleware import McpToolVisibilityMiddleware

logger = logging.getLogger(__name__)

BOOKKEEPING_UI_URI = "ui://moment-one/bookkeeping"
TIMELINE_UI_URI = "ui://moment-one/timeline"
HABITS_UI_URI = "ui://moment-one/habits"

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

_FALLBACK_TIMELINE_HTML = """<!DOCTYPE html><html lang="zh-CN"><body>
<div style="font-family:system-ui;padding:16px">
Moment One 时间线 UI 尚未构建，请使用 moments_list / moments_search 工具。
</div>
</body></html>"""

_FALLBACK_HABITS_HTML = """<!DOCTYPE html><html lang="zh-CN"><body>
<div style="font-family:system-ui;padding:16px">
Moment One 习惯 UI 尚未构建，请使用 habit_progress 工具。
</div>
</body></html>"""

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
    "bookkeeping_plan": (
        "记账意图解析（眼镜端预路由）：输入用户原话，返回 action=summary/create/list 与"
        "对应参数（含相对周期 year/month 换算）。眼镜端先调本工具再执行对应工具。"
    ),
    "moments_create": "创建一条通用或类型化 Moment。需要 moments.write；idempotencyKey 必填。",
    "moments_list": "按发生时间倒序浏览 Moment 时间线，支持类型、分类、标签和时间范围过滤。",
    "moments_search": "在标题、正文、摘要、标签、人物、事件和类型 payload 中搜索 Moment。",
    "moments_count": "统计指定时间范围内的 Moment 数量，并按分类和记录类型分组。",
    "reviews_daily": "生成某一天的回顾：记录数量、分类/类型分布和重点 Moment。",
    "moments_get": "按 momentId 查询单条完整 Moment（含 type/payload/provenance）。",
    "habit_goals_list": "列出当前用户的习惯目标。",
    "habit_goal_create": "创建一个每日或每周习惯目标，需要 moments.write。",
    "habit_checkin_create": "为一个习惯目标写入打卡 Moment，需要 moments.write 和 idempotencyKey。",
    "habit_progress": "返回习惯目标在最近若干天的完成情况、今日状态与连续天数。",
    "reminder_create": (
        "创建由 Moment One Server 调度的提醒。需要 moments.write、未来的带时区 remindAt "
        "以及 idempotencyKey；即使前端未打开，到期后也会按账号通知设置投递。"
    ),
    "agent_plan": (
        "把用户完整原话规划为一个当前已注册、参数合法且 Scope 允许的真实 MCP Tool。"
        "只返回 toolName/arguments/reply，不直接伪造业务结果。"
    ),
    "a2ui_action": "处理 A2UI 白名单只读 Action，返回后续真实 Tool 计划，不直接执行任意工具。",
    "account_entitlements": "查看当前账号套餐、存储和 MCP/Planner/AI 等额度，不消耗商业调用额度。",
}

# 远程提示词：眼镜端 LanguageModel 的记账助手指令（工具与提示词均由远程提供，
# 眼镜端只做客户端适配——动态声明工具 + 拉取本提示词，不内置记账规则）
BOOKKEEPING_PROMPT_NAME = "bookkeeping-assistant"
_BOOKKEEPING_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "bookkeeping_assistant.md"

# 提示词文件缺失时的降级内容（正常随容器部署，不应触发）
_BOOKKEEPING_PROMPT_FALLBACK = (
    "你是「一刻」的记账助手。只依据 MCP 工具返回的结果回答，禁止虚构结果。"
    "「记一笔/记账/花了/消费 xx 元」→ bookkeeping_create；"
    "「本月/上个月/某月/今年/去年花了多少、收支、结余」→ bookkeeping_summary"
    "（相对时间换算 year/month）；「明细/账单/某分类消费」→ bookkeeping_list。"
    "只回传工具实际返回的内容；工具报错时说明错误码，不要假装成功。每轮最多一个工具。"
)


def _load_bookkeeping_prompt() -> str:
    try:
        if _BOOKKEEPING_PROMPT_PATH.is_file():
            return _BOOKKEEPING_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return _BOOKKEEPING_PROMPT_FALLBACK


async def _call_with_a2ui(
    env: McpToolEnv,
    mcp_ctx: Context,
    fn: Callable[[tools.McpCallContext], Awaitable[object]],
    *,
    tool_name: str,
    idempotency_key: str | None = None,
) -> object:
    return await env.call(
        fn,
        tool_name=tool_name,
        idempotency_key=idempotency_key,
        a2ui_support=negotiate_a2ui(mcp_ctx.client_capabilities),
    )


def build_mcp_server(
    *,
    env: McpToolEnv,
    apps_html: str | None = None,
    timeline_html: str | None = None,
    habits_html: str | None = None,
    token_verifier: TokenVerifier | None = None,
    auth: AuthSettings | None = None,
) -> MCPServer:
    apps = Apps()
    # 顺序约束：Apps 扩展在 MCPServer 构造时应用（工具/资源注册），
    # 因此绑定 ui:// 的工具和 HTML 资源必须先注册进 apps 实例。
    _register_bookkeeping_create(apps, env)
    _register_bookkeeping_list(apps, env)
    _register_bookkeeping_summary(apps, env)
    _register_moment_app_tools(apps, env)
    _register_habit_app_tools(apps, env)
    apps.add_html_resource(
        BOOKKEEPING_UI_URI,
        apps_html or _FALLBACK_APPS_HTML,
        name="Moment One 记账",
        title="记账",
        description="记账记录列表 + 收支统计（ui://moment-one/bookkeeping）",
    )
    apps.add_html_resource(
        TIMELINE_UI_URI,
        timeline_html or _FALLBACK_TIMELINE_HTML,
        name="Moment One 记忆时间线",
        title="记忆时间线",
        description="时间线、搜索、详情和每日回顾（ui://moment-one/timeline）",
    )
    apps.add_html_resource(
        HABITS_UI_URI,
        habits_html or _FALLBACK_HABITS_HTML,
        name="Moment One 习惯",
        title="习惯追踪",
        description="习惯目标、七日进度与快捷打卡（ui://moment-one/habits）",
    )
    for uri, html in (
        (BOOKKEEPING_UI_URI, apps_html),
        (TIMELINE_UI_URI, timeline_html),
        (HABITS_UI_URI, habits_html),
    ):
        if not html:
            logger.warning("%s 使用降级占位资源", uri)

    server = MCPServer(
        name="moment-one-mcp",
        title="Moment One MCP Server",
        description="Moment One 个人生活记忆系统：记录、搜索、回顾、记账与习惯工具。",
        version="0.2.0",
        extensions=[apps, A2UIExtension()],
        token_verifier=token_verifier,
        auth=auth,
    )

    # 非 Apps 绑定工具在 server 构造后注册
    _register_bookkeeping_plan(server, env)
    _register_moments_count(server, env)
    _register_agent_plan(server, env)
    _register_a2ui_action(server, env)
    _register_account_entitlements(server, env)
    _register_reminder_create(server, env)
    _register_bookkeeping_prompt(server)
    server.middleware.append(McpToolVisibilityMiddleware(env))

    return server


# ---------------------------------------------------------------------------
# 工具：bookkeeping_plan（记账意图确定性解析）
# ---------------------------------------------------------------------------


def _register_bookkeeping_plan(server: MCPServer, env: McpToolEnv) -> None:
    @server.tool(
        name="bookkeeping_plan",
        description=_TOOL_DESCRIPTIONS["bookkeeping_plan"],
        title="记账意图解析",
    )
    async def bookkeeping_plan(  # pyright: ignore[reportUnusedFunction]
        input: Annotated[
            str, Field(description="用户原话（如：上个月花了多少 / 记一笔午餐 28.5 元）")
        ],
    ) -> object:
        return await env.call(
            lambda ctx: tools.bookkeeping_plan(ctx, input=input),
            tool_name="bookkeeping_plan",
        )


# ---------------------------------------------------------------------------
# 远程提示词：bookkeeping-assistant（眼镜端 LanguageModel 记账指令）
# ---------------------------------------------------------------------------


def _register_bookkeeping_prompt(server: MCPServer) -> None:
    """注册记账助手提示词（prompts/list + prompts/get），供眼镜端拉取。"""

    @server.prompt(
        name=BOOKKEEPING_PROMPT_NAME,
        title="记账助手",
        description="指导模型使用记账工具：记一笔、查统计（含相对周期换算）、查明细；只回传工具实际结果。",
    )
    async def bookkeeping_assistant() -> list[object]:  # pyright: ignore[reportUnusedFunction]
        return [
            {
                "role": "user",
                "content": {"type": "text", "text": _load_bookkeeping_prompt()},
            }
        ]


# ---------------------------------------------------------------------------
# bookkeeping_create（写，moments.write）
# ---------------------------------------------------------------------------


def _register_bookkeeping_create(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=BOOKKEEPING_UI_URI,
        name="bookkeeping_create",
        description=_TOOL_DESCRIPTIONS["bookkeeping_create"],
        title="记一笔账",
    )
    async def bookkeeping_create(  # pyright: ignore[reportUnusedFunction]
        mcp_ctx: Context,
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
        return await _call_with_a2ui(
            env,
            mcp_ctx,
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
            ),
            tool_name="bookkeeping_create",
            idempotency_key=idempotencyKey,
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
        mcp_ctx: Context,
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
        return await _call_with_a2ui(
            env,
            mcp_ctx,
            lambda ctx: tools.bookkeeping_list(
                ctx,
                limit=limit,
                cursor=cursor,
                from_=from_,
                to=to,
                category=category,
                ledger=ledger,
            ),
            tool_name="bookkeeping_list",
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
        mcp_ctx: Context,
        period: Annotated[
            str,
            Field(
                description="统计周期：month/quarter/year，或 custom（配合 from_/to 自定义范围）"
            ),
        ],
        year: Annotated[int | None, Field(default=None, description="年份（默认当前年）")] = None,
        month: Annotated[
            int | None,
            Field(default=None, description="月份（period=month 时 1~12；period=quarter 时 1~4）"),
        ] = None,
        ledger: Annotated[str | None, Field(default=None, description="按账本过滤")] = None,
        category: Annotated[
            str | None, Field(default=None, description="按分类过滤（分类小计）")
        ] = None,
        from_: Annotated[
            str | None,
            Field(
                default=None,
                description="自定义范围开始（ISO-8601，如今天 0 点；传了则优先于 period）",
            ),
        ] = None,
        to: Annotated[
            str | None,
            Field(default=None, description="自定义范围结束（ISO-8601，开区间）"),
        ] = None,
    ) -> object:
        return await _call_with_a2ui(
            env,
            mcp_ctx,
            lambda ctx: tools.bookkeeping_summary(
                ctx,
                period=period,
                year=year,
                month=month,
                ledger=ledger,
                category=category,
                from_=from_,
                to=to,
            ),
            tool_name="bookkeeping_summary",
        )


# ---------------------------------------------------------------------------
# moments_get（读，moments.read）
# ---------------------------------------------------------------------------


def _register_moment_app_tools(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=TIMELINE_UI_URI,
        name="moments_list",
        description=_TOOL_DESCRIPTIONS["moments_list"],
        title="浏览记忆时间线",
    )
    async def moments_list(  # pyright: ignore[reportUnusedFunction]
        mcp_ctx: Context,
        limit: Annotated[int, Field(default=20, ge=1, le=20)] = 20,
        cursor: Annotated[str | None, Field(default=None, description="上一页 nextCursor")] = None,
        type: Annotated[
            str | None, Field(default=None, description="记录类型，如 general/habit/bookkeeping")
        ] = None,
        category: Annotated[str | None, Field(default=None, description="Moment 分类")] = None,
        tag: Annotated[str | None, Field(default=None, description="标签")] = None,
        from_: Annotated[str | None, Field(default=None, description="开始时间 ISO-8601")] = None,
        to: Annotated[str | None, Field(default=None, description="结束时间 ISO-8601")] = None,
    ) -> object:
        return await _call_with_a2ui(
            env,
            mcp_ctx,
            lambda ctx: tools.moments_list(
                ctx,
                limit=limit,
                cursor=cursor,
                moment_type=type,
                category=category,
                tag=tag,
                from_=from_,
                to=to,
            ),
            tool_name="moments_list",
        )

    @apps.tool(
        resource_uri=TIMELINE_UI_URI,
        name="moments_search",
        description=_TOOL_DESCRIPTIONS["moments_search"],
        title="搜索记忆",
    )
    async def moments_search(  # pyright: ignore[reportUnusedFunction]
        mcp_ctx: Context,
        query: Annotated[str, Field(min_length=1, description="搜索词或短语")],
        limit: Annotated[int, Field(default=20, ge=1, le=20)] = 20,
        type: Annotated[str | None, Field(default=None, description="记录类型过滤")] = None,
        category: Annotated[str | None, Field(default=None, description="分类过滤")] = None,
        from_: Annotated[str | None, Field(default=None, description="开始时间 ISO-8601")] = None,
        to: Annotated[str | None, Field(default=None, description="结束时间 ISO-8601")] = None,
    ) -> object:
        return await _call_with_a2ui(
            env,
            mcp_ctx,
            lambda ctx: tools.moments_search(
                ctx,
                query=query,
                limit=limit,
                moment_type=type,
                category=category,
                from_=from_,
                to=to,
            ),
            tool_name="moments_search",
        )

    @apps.tool(
        resource_uri=TIMELINE_UI_URI,
        name="reviews_daily",
        description=_TOOL_DESCRIPTIONS["reviews_daily"],
        title="每日回顾",
    )
    async def reviews_daily(  # pyright: ignore[reportUnusedFunction]
        mcp_ctx: Context,
        date: Annotated[
            str | None, Field(default=None, description="日期 YYYY-MM-DD；默认今天")
        ] = None,
        timezone: Annotated[
            str | None, Field(default=None, description="IANA 时区，如 Asia/Shanghai")
        ] = None,
    ) -> object:
        return await _call_with_a2ui(
            env,
            mcp_ctx,
            lambda ctx: tools.reviews_daily(ctx, day=date, timezone_name=timezone),
            tool_name="reviews_daily",
        )

    @apps.tool(
        resource_uri=TIMELINE_UI_URI,
        name="moments_get",
        description=_TOOL_DESCRIPTIONS["moments_get"],
        title="查看 Moment 详情",
    )
    async def moments_get(  # pyright: ignore[reportUnusedFunction]
        mcp_ctx: Context,
        momentId: Annotated[str, Field(description="Moment ID（UUID）")],
    ) -> object:
        return await _call_with_a2ui(
            env,
            mcp_ctx,
            lambda ctx: tools.moments_get(ctx, moment_id=momentId),
            tool_name="moments_get",
        )

    @apps.tool(
        resource_uri=TIMELINE_UI_URI,
        name="moments_create",
        description=_TOOL_DESCRIPTIONS["moments_create"],
        title="记录一个 Moment",
    )
    async def moments_create(  # pyright: ignore[reportUnusedFunction]
        title: Annotated[str, Field(min_length=1, max_length=20)],
        idempotencyKey: Annotated[str, Field(min_length=8, description="客户端生成的幂等键")],
        description: Annotated[str | None, Field(default=None, max_length=240)] = None,
        category: Annotated[
            Literal["experience", "habit", "travel", "food", "growth", "emotion"],
            Field(default="experience"),
        ] = "experience",
        tags: Annotated[list[str] | None, Field(default=None, max_length=5)] = None,
        persons: Annotated[list[str] | None, Field(default=None, max_length=10)] = None,
        event: Annotated[str | None, Field(default=None, max_length=50)] = None,
        occurredAt: Annotated[
            str | None, Field(default=None, description="ISO-8601；默认当前时间")
        ] = None,
        timezone: Annotated[str, Field(default="UTC", description="IANA 时区")] = "UTC",
        type: Annotated[str, Field(default="general")] = "general",
        payload: Annotated[dict | None, Field(default=None)] = None,
    ) -> object:
        return await env.call(
            lambda ctx: tools.moments_create(
                ctx,
                title=title,
                description=description,
                category=category,
                tags=tags,
                persons=persons,
                event=event,
                occurred_at=occurredAt,
                timezone_name=timezone,
                moment_type=type,
                payload=payload,
                idempotency_key=idempotencyKey,
            ),
            tool_name="moments_create",
            idempotency_key=idempotencyKey,
        )


def _register_habit_app_tools(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=HABITS_UI_URI,
        name="habit_progress",
        description=_TOOL_DESCRIPTIONS["habit_progress"],
        title="查看习惯进度",
    )
    async def habit_progress(  # pyright: ignore[reportUnusedFunction]
        mcp_ctx: Context,
        days: Annotated[int, Field(default=7, ge=7, le=31)] = 7,
        timezone: Annotated[str | None, Field(default=None, description="IANA 时区")] = None,
    ) -> object:
        return await _call_with_a2ui(
            env,
            mcp_ctx,
            lambda ctx: tools.habit_progress(ctx, days=days, timezone_name=timezone),
            tool_name="habit_progress",
        )

    @apps.tool(
        resource_uri=HABITS_UI_URI,
        name="habit_goals_list",
        description=_TOOL_DESCRIPTIONS["habit_goals_list"],
        title="习惯目标列表",
    )
    async def habit_goals_list() -> object:  # pyright: ignore[reportUnusedFunction]
        return await env.call(tools.habit_goals_list, tool_name="habit_goals_list")

    @apps.tool(
        resource_uri=HABITS_UI_URI,
        name="habit_goal_create",
        description=_TOOL_DESCRIPTIONS["habit_goal_create"],
        title="创建习惯目标",
    )
    async def habit_goal_create(  # pyright: ignore[reportUnusedFunction]
        name: Annotated[str, Field(min_length=1, max_length=30)],
        idempotencyKey: Annotated[str, Field(min_length=8, description="客户端生成的幂等键")],
        frequency: Annotated[Literal["daily", "weekly"], Field(default="daily")] = "daily",
        unit: Annotated[str | None, Field(default=None, max_length=20)] = None,
        timesPerWeek: Annotated[int | None, Field(default=None, ge=1, le=7)] = None,
        color: Annotated[str | None, Field(default=None, max_length=16)] = None,
    ) -> object:
        return await env.call(
            lambda ctx: tools.habit_goal_create(
                ctx,
                name=name,
                unit=unit,
                frequency=frequency,
                times_per_week=timesPerWeek,
                color=color,
                idempotency_key=idempotencyKey,
            ),
            tool_name="habit_goal_create",
            idempotency_key=idempotencyKey,
        )

    @apps.tool(
        resource_uri=HABITS_UI_URI,
        name="habit_checkin_create",
        description=_TOOL_DESCRIPTIONS["habit_checkin_create"],
        title="习惯打卡",
    )
    async def habit_checkin_create(  # pyright: ignore[reportUnusedFunction]
        mcp_ctx: Context,
        goalId: Annotated[str, Field(description="习惯目标 UUID")],
        idempotencyKey: Annotated[str, Field(min_length=8)],
        done: Annotated[bool, Field(default=True)] = True,
        count: Annotated[int | None, Field(default=None, ge=1)] = None,
        occurredAt: Annotated[str | None, Field(default=None)] = None,
        timezone: Annotated[str, Field(default="UTC")] = "UTC",
        note: Annotated[str | None, Field(default=None, max_length=240)] = None,
    ) -> object:
        return await _call_with_a2ui(
            env,
            mcp_ctx,
            lambda ctx: tools.habit_checkin_create(
                ctx,
                goal_id=goalId,
                done=done,
                count=count,
                occurred_at=occurredAt,
                timezone_name=timezone,
                note=note,
                idempotency_key=idempotencyKey,
            ),
            tool_name="habit_checkin_create",
            idempotency_key=idempotencyKey,
        )


def _register_moments_count(server: MCPServer, env: McpToolEnv) -> None:
    @server.tool(
        name="moments_count",
        description=_TOOL_DESCRIPTIONS["moments_count"],
        title="统计 Moment 数量",
    )
    async def moments_count(  # pyright: ignore[reportUnusedFunction]
        type: Annotated[str | None, Field(default=None)] = None,
        category: Annotated[str | None, Field(default=None)] = None,
        from_: Annotated[str | None, Field(default=None)] = None,
        to: Annotated[str | None, Field(default=None)] = None,
    ) -> object:
        return await env.call(
            lambda ctx: tools.moments_count(
                ctx,
                moment_type=type,
                category=category,
                from_=from_,
                to=to,
            ),
            tool_name="moments_count",
        )


def _registered_tool_schemas(server: MCPServer) -> dict[str, dict]:
    return {
        tool.name: tool.parameters
        for tool in server._tool_manager.list_tools()  # pyright: ignore[reportPrivateUsage]
    }


def _register_agent_plan(server: MCPServer, env: McpToolEnv) -> None:
    @server.tool(
        name="agent_plan",
        description=_TOOL_DESCRIPTIONS["agent_plan"],
        title="通用工具规划",
    )
    async def agent_plan(  # pyright: ignore[reportUnusedFunction]
        input: Annotated[str, Field(min_length=1, description="用户完整原话")],
    ) -> object:
        return await env.call(
            lambda ctx: tools.agent_plan(
                ctx,
                input=input,
                tool_schemas=_registered_tool_schemas(server),
            ),
            tool_name="agent_plan",
        )


def _register_a2ui_action(server: MCPServer, env: McpToolEnv) -> None:
    @server.tool(
        name="a2ui_action",
        description=_TOOL_DESCRIPTIONS["a2ui_action"],
        title="A2UI Action",
    )
    async def a2ui_action(  # pyright: ignore[reportUnusedFunction]
        name: Annotated[Literal["open_detail", "refresh"], Field(description="白名单 Action")],
        context: Annotated[dict, Field(default_factory=dict, description="Action 上下文")],
        surfaceId: Annotated[str, Field(min_length=1, max_length=120)],
    ) -> object:
        return await env.call(
            lambda ctx: tools.a2ui_action(
                ctx,
                name=name,
                context=context,
                surface_id=surfaceId,
            ),
            tool_name="a2ui_action",
        )


def _register_account_entitlements(server: MCPServer, env: McpToolEnv) -> None:
    @server.tool(
        name="account_entitlements",
        description=_TOOL_DESCRIPTIONS["account_entitlements"],
        title="查看账号额度",
    )
    async def account_entitlements() -> object:  # pyright: ignore[reportUnusedFunction]
        return await env.call(
            tools.account_entitlements,
            tool_name="account_entitlements",
        )


def _register_reminder_create(server: MCPServer, env: McpToolEnv) -> None:
    @server.tool(
        name="reminder_create",
        description=_TOOL_DESCRIPTIONS["reminder_create"],
        title="创建提醒",
    )
    async def reminder_create(  # pyright: ignore[reportUnusedFunction]
        title: Annotated[str, Field(min_length=1, max_length=160)],
        remindAt: Annotated[str, Field(description="带时区的 ISO-8601 未来提醒时间")],
        timezone: Annotated[str, Field(description="IANA 时区，如 Asia/Shanghai")],
        idempotencyKey: Annotated[str, Field(min_length=8, max_length=160)],
        note: Annotated[str | None, Field(default=None, max_length=2000)] = None,
        scene: Annotated[
            Literal["general", "bookkeeping", "habit"], Field(default="general")
        ] = "general",
        dueAt: Annotated[str | None, Field(default=None, description="可选截止时间")] = None,
    ) -> object:
        return await env.call(
            lambda ctx: tools.reminder_create(
                ctx,
                title=title,
                note=note,
                scene=scene,
                remind_at=remindAt,
                deadline_at=dueAt,
                timezone_name=timezone,
                idempotency_key=idempotencyKey,
            ),
            tool_name="reminder_create",
            idempotency_key=idempotencyKey,
        )


__all__ = ["BOOKKEEPING_UI_URI", "TIMELINE_UI_URI", "HABITS_UI_URI", "build_mcp_server"]
