"""构建 Moment One MCP Server（mcp 官方 Python SDK）。

- Streamable HTTP（`POST /mcp`，挂载见 application.py）
- Bearer 鉴权由 `BearerAuthBackend(MomentTokenVerifier)` + `RequireAuthMiddleware` 处理
- 工具：记账 + 通用 Moment + 每日回顾 + 习惯目标/打卡
- MCP Apps：每个普通工具拥有独立 ui:// 壳，同业务域复用渲染内核
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Literal, cast
from urllib.parse import urlparse

from mcp.server.apps import Apps, ResourceCsp
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from app.modules.mcp import output_schemas as output
from app.modules.mcp import tools
from app.modules.mcp.a2ui import A2UIExtension, negotiate_a2ui
from app.modules.mcp.deps import McpToolEnv
from app.modules.mcp.quota_middleware import McpToolVisibilityMiddleware

logger = logging.getLogger(__name__)

MCP_APP_TOOL_SPECS: dict[str, tuple[str, str]] = {
    "bookkeeping_create": ("bookkeeping", "记账成功"),
    "bookkeeping_list": ("bookkeeping", "账目明细"),
    "bookkeeping_summary": ("bookkeeping", "收支概览"),
    "moments_create": ("timeline", "记录成功"),
    "moments_list": ("timeline", "最近的 Moment"),
    "moments_search": ("timeline", "搜索结果"),
    "moments_get": ("timeline", "Moment 详情"),
    "reviews_daily": ("timeline", "每日回顾"),
    "habit_goals_list": ("habits", "习惯目标"),
    "habit_goal_create": ("habits", "习惯已创建"),
    "habit_goal_update": ("habits", "习惯已更新"),
    "habit_checkin_create": ("habits", "打卡已记录"),
    "habit_progress": ("habits", "习惯进度"),
    "moments_count": ("utility", "Moment 统计"),
    "account_entitlements": ("utility", "账号额度"),
    "reminder_create": ("utility", "提醒已创建"),
    "feedback_submit": ("utility", "反馈已收到"),
    "asset_upload_intent_create": ("utility", "可以上传附件"),
    "asset_upload_complete": ("utility", "附件已就绪"),
}


def tool_ui_uri(tool_name: str) -> str:
    """普通 MCP Tool 与独立 Apps 资源的一对一稳定映射。"""
    if tool_name not in MCP_APP_TOOL_SPECS:
        raise KeyError(f"未注册 MCP App UI 的工具：{tool_name}")
    return f"ui://moment-one/tools/{tool_name}"


# MCP Apps 轻量启动壳；业务结果始终另有 content 文本降级。
def _app_shell(asset_url: str, title: str) -> str:
    """只承载 CDN 入口的 MCP App HTML；不内联 bundle 或用户数据。"""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title></head><body>"
        '<main id="root" aria-live="polite">正在加载 Moment One…</main>'
        f'<script type="module" src="{asset_url}"></script></body></html>'
    )


# 工具描述（供模型理解；错误码与 docs/contracts/MCP_SERVER.md §7 对齐）
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "bookkeeping_create": (
        "记录一笔账（bookkeeping）。需要 moments.write 权限。"
        "amount 金额、flow 流向（expense/income）为必填；occurredAt 省略时由 Server 记录接收时刻，"
        "补录过去账目时传带 offset 的 ISO-8601；timezone 省略时使用账号时区。"
        "category 分类（如餐饮、交通）、merchant 商家、ledger 账本、account 账户可选。"
        "可通过 assetIds 引用已 ready 且属于当前用户的附件。"
        "支持 idempotencyKey 幂等重试。非法 payload 返回 INVALID_ARGUMENTS。"
    ),
    "bookkeeping_list": (
        "按发生时间倒序列出记账记录。limit 不超过 20，返回多少条，UI 就展示多少条；"
        "支持 cursor 分页与 from/to/category/ledger 过滤。"
    ),
    "bookkeeping_summary": (
        "记账统计（服务端聚合）：周期内收支合计 + 分类小计，口径与 Web 记账板块一致。"
        "period 为 month/quarter/year，可指定 year/month（month 为月份或季度号）。"
    ),
    "bookkeeping_plan": (
        "记账意图解析（眼镜端预路由）：输入用户原话，返回 action=summary/create/list 与"
        "对应参数（含相对周期 year/month 换算）。眼镜端先调本工具再执行对应工具。"
    ),
    "moments_create": (
        "创建一条通用或类型化 Moment。可引用已 ready 且属于当前用户的 assetIds；"
        "需要 moments.write 和 idempotencyKey。"
    ),
    "moments_list": "按发生时间倒序浏览 Moment 时间线，支持类型、分类、标签和时间范围过滤。",
    "moments_search": "在标题、正文、摘要、标签、人物、事件和类型 payload 中搜索 Moment。",
    "moments_count": "统计指定时间范围内的 Moment 数量，并按分类和记录类型分组。",
    "reviews_daily": "生成某一天的回顾：记录数量、分类/类型分布和重点 Moment。",
    "moments_get": "按 momentId 查询单条完整 Moment（含 type/payload/provenance）。",
    "habit_goals_list": "列出当前用户的习惯目标。",
    "habit_goal_create": "创建一个每日或每周习惯目标，需要 moments.write。",
    "habit_goal_update": "修改习惯名称、单位或每日/每周/每月目标；必须传 expectedRevision。",
    "habit_checkin_create": (
        "为习惯目标写入打卡 Moment，可引用已 ready 的 assetIds；"
        "需要 moments.write 和 idempotencyKey。"
    ),
    "habit_progress": (
        "返回指定习惯（goalId）或全部习惯在最近若干天的完成情况、今日状态与连续天数。"
    ),
    "reminder_create": (
        "创建由 Moment One Server 调度的提醒。时间三选一：带 offset 的 remindAt、"
        "localDateTime + IANA timezone、或 afterMinutes；不要自行把用户本地时间换算成 UTC。"
        "timezone 省略时使用账号时区。日期或时间语义不明确时先询问用户。"
        "需要 moments.write 和 idempotencyKey；到期后按账号通知设置投递。"
    ),
    "agent_plan": (
        "把用户完整原话规划为一个当前已注册、参数合法且 Scope 允许的真实 MCP Tool。"
        "只返回 toolName/arguments/reply，不直接伪造业务结果。"
    ),
    "a2ui_action": "处理 A2UI 白名单只读 Action，返回后续真实 Tool 计划，不直接执行任意工具。",
    "account_entitlements": "查看当前账号套餐、存储和 MCP/Planner/AI 等额度，不消耗商业调用额度。",
    "feedback_submit": (
        "提交用户对 Moment One 的问题、体验反馈或功能建议。仅在用户明确表达反馈意愿时调用。"
    ),
    "asset_upload_intent_create": (
        "为用户明确提供的本地附件创建短期直传意图。传 MIME 和精确字节数，返回预签名 PUT URL；"
        "不要传 Base64，也不要让 Server 代抓任意公网 URL。需要 moments.write 和 media.upload。"
    ),
    "asset_upload_complete": (
        "客户端 PUT 完成后确认附件，Server 校验对象大小、类型和所有权并返回 ready assetId。"
        "只有 ready assetId 才能传给记录类工具。"
    ),
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
    apps_asset_base_url: str = "https://moment-one.yuanshuai.fun/mcp-apps",
    apps_version: str = "v1",
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
    _register_moments_count(apps, env)
    _register_account_entitlements(apps, env)
    _register_reminder_create(apps, env)
    _register_feedback_submit(apps, env)
    _register_asset_tools(apps, env)
    asset_root = f"{apps_asset_base_url.rstrip('/')}/{apps_version.strip('/')}"
    parsed_asset_root = urlparse(asset_root)
    if parsed_asset_root.scheme not in {"http", "https"} or not parsed_asset_root.netloc:
        raise ValueError("MCP Apps asset base URL 必须是带 origin 的 HTTP(S) URL")
    resource_origin = f"{parsed_asset_root.scheme}://{parsed_asset_root.netloc}"
    csp = ResourceCsp(resource_domains=[resource_origin])
    for tool_name, (renderer, title) in MCP_APP_TOOL_SPECS.items():
        resource_uri = tool_ui_uri(tool_name)
        apps.add_html_resource(
            resource_uri,
            _app_shell(f"{asset_root}/assets/{renderer}.js", f"Moment One · {title}"),
            name=f"Moment One · {tool_name}",
            title=title,
            description=f"{tool_name} 的独立返回 UI（{resource_uri}）",
            csp=csp,
            domain=resource_origin,
        )

    server = MCPServer(
        name="moment-one-mcp",
        title="Moment One MCP Server",
        description="Moment One 个人生活记忆系统：记录、搜索、回顾、记账与习惯工具。",
        version="0.2.0",
        extensions=[apps, A2UIExtension()],
        token_verifier=token_verifier,
        auth=auth,
        # Must precede extension interceptors: Apps handles tools/list itself.
        middleware=[McpToolVisibilityMiddleware(env)],
    )

    # 非 Apps 绑定工具在 server 构造后注册
    _register_bookkeeping_plan(server, env)
    _register_agent_plan(server, env)
    _register_a2ui_action(server, env)
    _register_bookkeeping_prompt(server)
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
        resource_uri=tool_ui_uri("bookkeeping_create"),
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
            str | None,
            Field(default=None, description="带 offset 的事实发生时间；省略表示服务器接收时刻"),
        ] = None,
        occurredLocalDateTime: Annotated[
            str | None,
            Field(
                default=None, description="补录用本地时间，需配合 timezone；与 occurredAt 二选一"
            ),
        ] = None,
        timezone: Annotated[
            str | None, Field(default=None, description="IANA 时区；默认账号时区")
        ] = None,
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
            str | None,
            Field(
                default=None,
                min_length=8,
                max_length=160,
                description="客户端生成的幂等键；新客户端必须传，暂兼容旧眼镜客户端省略",
            ),
        ] = None,
        title: Annotated[
            str | None,
            Field(default=None, max_length=20, description="标题（可选，默认取分类/商家）"),
        ] = None,
        assetIds: Annotated[
            list[str] | None,
            Field(default=None, max_length=10, description="已 ready 的当前用户附件 UUID"),
        ] = None,
    ) -> output.BookkeepingCreateOutput:
        return cast(
            output.BookkeepingCreateOutput,
            await _call_with_a2ui(
                env,
                mcp_ctx,
                lambda ctx: tools.bookkeeping_create(
                    ctx,
                    amount=amount,
                    flow=flow,
                    occurred_at=occurredAt,
                    occurred_local_date_time=occurredLocalDateTime,
                    timezone_name=timezone,
                    account=account,
                    category=category,
                    merchant=merchant,
                    ledger=ledger,
                    method=method,
                    count_in_flow=countInFlow,
                    count_in_budget=countInBudget,
                    idempotency_key=idempotencyKey,
                    title=title,
                    asset_ids=assetIds,
                ),
                tool_name="bookkeeping_create",
                idempotency_key=idempotencyKey,
            ),
        )


# ---------------------------------------------------------------------------
# bookkeeping_list（读，moments.read，绑定 Apps UI）
# ---------------------------------------------------------------------------


def _register_bookkeeping_list(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=tool_ui_uri("bookkeeping_list"),
        name="bookkeeping_list",
        description=_TOOL_DESCRIPTIONS["bookkeeping_list"],
        title="记账记录列表",
    )
    async def bookkeeping_list(  # pyright: ignore[reportUnusedFunction]
        mcp_ctx: Context,
        limit: Annotated[
            int, Field(default=20, ge=1, le=20, description="本次展示数量（≤20）")
        ] = 20,
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
    ) -> output.BookkeepingListOutput:
        return cast(
            output.BookkeepingListOutput,
            await _call_with_a2ui(
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
            ),
        )


# ---------------------------------------------------------------------------
# bookkeeping_summary（读，moments.read，绑定 Apps UI）
# ---------------------------------------------------------------------------


def _register_bookkeeping_summary(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=tool_ui_uri("bookkeeping_summary"),
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
    ) -> output.BookkeepingSummaryOutput:
        return cast(
            output.BookkeepingSummaryOutput,
            await _call_with_a2ui(
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
            ),
        )


# ---------------------------------------------------------------------------
# moments_get（读，moments.read）
# ---------------------------------------------------------------------------


def _register_moment_app_tools(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=tool_ui_uri("moments_list"),
        name="moments_list",
        description=_TOOL_DESCRIPTIONS["moments_list"],
        title="浏览记忆时间线",
    )
    async def moments_list(  # pyright: ignore[reportUnusedFunction]
        mcp_ctx: Context,
        limit: Annotated[
            int, Field(default=20, ge=1, le=20, description="本次展示数量（≤20）")
        ] = 20,
        cursor: Annotated[str | None, Field(default=None, description="上一页 nextCursor")] = None,
        type: Annotated[
            str | None, Field(default=None, description="记录类型，如 general/habit/bookkeeping")
        ] = None,
        category: Annotated[str | None, Field(default=None, description="Moment 分类")] = None,
        tag: Annotated[str | None, Field(default=None, description="标签")] = None,
        from_: Annotated[str | None, Field(default=None, description="开始时间 ISO-8601")] = None,
        to: Annotated[str | None, Field(default=None, description="结束时间 ISO-8601")] = None,
    ) -> output.MomentListOutput:
        return cast(
            output.MomentListOutput,
            await _call_with_a2ui(
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
            ),
        )

    @apps.tool(
        resource_uri=tool_ui_uri("moments_search"),
        name="moments_search",
        description=_TOOL_DESCRIPTIONS["moments_search"],
        title="搜索记忆",
    )
    async def moments_search(  # pyright: ignore[reportUnusedFunction]
        mcp_ctx: Context,
        query: Annotated[str, Field(min_length=1, description="搜索词或短语")],
        limit: Annotated[
            int, Field(default=20, ge=1, le=20, description="本次展示数量（≤20）")
        ] = 20,
        type: Annotated[str | None, Field(default=None, description="记录类型过滤")] = None,
        category: Annotated[str | None, Field(default=None, description="分类过滤")] = None,
        from_: Annotated[str | None, Field(default=None, description="开始时间 ISO-8601")] = None,
        to: Annotated[str | None, Field(default=None, description="结束时间 ISO-8601")] = None,
    ) -> output.MomentSearchOutput:
        return cast(
            output.MomentSearchOutput,
            await _call_with_a2ui(
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
            ),
        )

    @apps.tool(
        resource_uri=tool_ui_uri("reviews_daily"),
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
    ) -> output.DailyReviewOutput:
        return cast(
            output.DailyReviewOutput,
            await _call_with_a2ui(
                env,
                mcp_ctx,
                lambda ctx: tools.reviews_daily(ctx, day=date, timezone_name=timezone),
                tool_name="reviews_daily",
            ),
        )

    @apps.tool(
        resource_uri=tool_ui_uri("moments_get"),
        name="moments_get",
        description=_TOOL_DESCRIPTIONS["moments_get"],
        title="查看 Moment 详情",
    )
    async def moments_get(  # pyright: ignore[reportUnusedFunction]
        mcp_ctx: Context,
        momentId: Annotated[str, Field(description="Moment ID（UUID）")],
    ) -> output.MomentGetOutput:
        return cast(
            output.MomentGetOutput,
            await _call_with_a2ui(
                env,
                mcp_ctx,
                lambda ctx: tools.moments_get(ctx, moment_id=momentId),
                tool_name="moments_get",
            ),
        )

    @apps.tool(
        resource_uri=tool_ui_uri("moments_create"),
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
            str | None,
            Field(default=None, description="带 offset 的事实发生时间；默认服务器接收时刻"),
        ] = None,
        occurredLocalDateTime: Annotated[
            str | None,
            Field(
                default=None, description="补录用本地时间，需配合 timezone；与 occurredAt 二选一"
            ),
        ] = None,
        timezone: Annotated[
            str | None, Field(default=None, description="IANA 时区；默认账号时区")
        ] = None,
        type: Annotated[str, Field(default="general")] = "general",
        payload: Annotated[dict | None, Field(default=None)] = None,
        assetIds: Annotated[
            list[str] | None,
            Field(default=None, max_length=10, description="已 ready 的当前用户附件 UUID"),
        ] = None,
    ) -> output.MomentCreateOutput:
        return cast(
            output.MomentCreateOutput,
            await env.call(
                lambda ctx: tools.moments_create(
                    ctx,
                    title=title,
                    description=description,
                    category=category,
                    tags=tags,
                    persons=persons,
                    event=event,
                    occurred_at=occurredAt,
                    occurred_local_date_time=occurredLocalDateTime,
                    timezone_name=timezone,
                    moment_type=type,
                    payload=payload,
                    idempotency_key=idempotencyKey,
                    asset_ids=assetIds,
                ),
                tool_name="moments_create",
                idempotency_key=idempotencyKey,
            ),
        )


def _register_habit_app_tools(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=tool_ui_uri("habit_progress"),
        name="habit_progress",
        description=_TOOL_DESCRIPTIONS["habit_progress"],
        title="查看习惯进度",
    )
    async def habit_progress(  # pyright: ignore[reportUnusedFunction]
        mcp_ctx: Context,
        days: Annotated[int, Field(default=7, ge=7, le=31)] = 7,
        goalId: Annotated[
            str | None, Field(default=None, description="只查看一个习惯目标（UUID）")
        ] = None,
        timezone: Annotated[str | None, Field(default=None, description="IANA 时区")] = None,
    ) -> output.HabitProgressOutput:
        return cast(
            output.HabitProgressOutput,
            await _call_with_a2ui(
                env,
                mcp_ctx,
                lambda ctx: tools.habit_progress(
                    ctx, days=days, timezone_name=timezone, goal_id=goalId
                ),
                tool_name="habit_progress",
            ),
        )

    @apps.tool(
        resource_uri=tool_ui_uri("habit_goals_list"),
        name="habit_goals_list",
        description=_TOOL_DESCRIPTIONS["habit_goals_list"],
        title="习惯目标列表",
    )
    async def habit_goals_list() -> output.HabitGoalsListOutput:  # pyright: ignore[reportUnusedFunction]
        return cast(
            output.HabitGoalsListOutput,
            await env.call(tools.habit_goals_list, tool_name="habit_goals_list"),
        )

    @apps.tool(
        resource_uri=tool_ui_uri("habit_goal_create"),
        name="habit_goal_create",
        description=_TOOL_DESCRIPTIONS["habit_goal_create"],
        title="创建习惯目标",
    )
    async def habit_goal_create(  # pyright: ignore[reportUnusedFunction]
        name: Annotated[str, Field(min_length=1, max_length=30)],
        idempotencyKey: Annotated[str, Field(min_length=8, description="客户端生成的幂等键")],
        frequency: Annotated[
            Literal["daily", "weekly", "monthly"], Field(default="daily")
        ] = "daily",
        unit: Annotated[str | None, Field(default=None, max_length=20)] = None,
        timesPerWeek: Annotated[int | None, Field(default=None, ge=1, le=7)] = None,
        targetPeriod: Annotated[
            Literal["daily", "weekly", "monthly"] | None, Field(default=None)
        ] = None,
        targetCount: Annotated[int | None, Field(default=None, ge=1, le=366)] = None,
        color: Annotated[str | None, Field(default=None, max_length=16)] = None,
        reminderLocalTime: Annotated[
            str | None,
            Field(default=None, description="可选首次习惯提醒本地时间 YYYY-MM-DDTHH:MM[:SS]"),
        ] = None,
        reminderTimezone: Annotated[
            str | None, Field(default=None, description="提醒 IANA 时区；默认账号时区")
        ] = None,
    ) -> output.HabitGoalCreateOutput:
        async def create_with_optional_reminder(ctx: tools.McpCallContext) -> object:
            result = await tools.habit_goal_create(
                ctx,
                name=name,
                unit=unit,
                frequency=frequency,
                times_per_week=timesPerWeek,
                target_period=targetPeriod,
                target_count=targetCount,
                color=color,
                idempotency_key=idempotencyKey,
            )
            if reminderLocalTime:
                await tools.habit_reminder_create_for_goal(
                    ctx,
                    goal_result=result,
                    local_date_time=reminderLocalTime,
                    timezone_name=reminderTimezone,
                    idempotency_key=f"{idempotencyKey}:reminder",
                )
            return result

        return cast(
            output.HabitGoalCreateOutput,
            await env.call(
                create_with_optional_reminder,
                tool_name="habit_goal_create",
                idempotency_key=idempotencyKey,
            ),
        )

    @apps.tool(
        resource_uri=tool_ui_uri("habit_goal_update"),
        name="habit_goal_update",
        description=_TOOL_DESCRIPTIONS["habit_goal_update"],
        title="修改习惯目标",
    )
    async def habit_goal_update(  # pyright: ignore[reportUnusedFunction]
        mcp_ctx: Context,
        goalId: Annotated[str, Field(description="习惯目标 UUID")],
        expectedRevision: Annotated[int, Field(ge=1)],
        name: Annotated[str | None, Field(default=None, min_length=1, max_length=30)] = None,
        unit: Annotated[str | None, Field(default=None, max_length=20)] = None,
        targetPeriod: Annotated[
            Literal["daily", "weekly", "monthly"] | None, Field(default=None)
        ] = None,
        targetCount: Annotated[int | None, Field(default=None, ge=1, le=366)] = None,
        color: Annotated[str | None, Field(default=None, max_length=16)] = None,
    ) -> output.HabitGoalUpdateOutput:
        return cast(
            output.HabitGoalUpdateOutput,
            await _call_with_a2ui(
                env,
                mcp_ctx,
                lambda ctx: tools.habit_goal_update(
                    ctx,
                    goal_id=goalId,
                    expected_revision=expectedRevision,
                    name=name,
                    unit=unit,
                    target_period=targetPeriod,
                    target_count=targetCount,
                    color=color,
                ),
                tool_name="habit_goal_update",
            ),
        )

    @apps.tool(
        resource_uri=tool_ui_uri("habit_checkin_create"),
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
        assetIds: Annotated[
            list[str] | None,
            Field(default=None, max_length=10, description="已 ready 的当前用户附件 UUID"),
        ] = None,
    ) -> output.HabitCheckinOutput:
        return cast(
            output.HabitCheckinOutput,
            await _call_with_a2ui(
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
                    asset_ids=assetIds,
                ),
                tool_name="habit_checkin_create",
                idempotency_key=idempotencyKey,
            ),
        )


def _register_moments_count(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=tool_ui_uri("moments_count"),
        name="moments_count",
        description=_TOOL_DESCRIPTIONS["moments_count"],
        title="统计 Moment 数量",
    )
    async def moments_count(  # pyright: ignore[reportUnusedFunction]
        type: Annotated[str | None, Field(default=None)] = None,
        category: Annotated[str | None, Field(default=None)] = None,
        from_: Annotated[str | None, Field(default=None)] = None,
        to: Annotated[str | None, Field(default=None)] = None,
    ) -> output.MomentCountOutput:
        return cast(
            output.MomentCountOutput,
            await env.call(
                lambda ctx: tools.moments_count(
                    ctx,
                    moment_type=type,
                    category=category,
                    from_=from_,
                    to=to,
                ),
                tool_name="moments_count",
            ),
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


def _register_account_entitlements(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=tool_ui_uri("account_entitlements"),
        name="account_entitlements",
        description=_TOOL_DESCRIPTIONS["account_entitlements"],
        title="查看账号额度",
    )
    async def account_entitlements() -> output.AccountEntitlementsOutput:  # pyright: ignore[reportUnusedFunction]
        return cast(
            output.AccountEntitlementsOutput,
            await env.call(
                tools.account_entitlements,
                tool_name="account_entitlements",
            ),
        )


def _register_reminder_create(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=tool_ui_uri("reminder_create"),
        name="reminder_create",
        description=_TOOL_DESCRIPTIONS["reminder_create"],
        title="创建提醒",
    )
    async def reminder_create(  # pyright: ignore[reportUnusedFunction]
        title: Annotated[str, Field(min_length=1, max_length=160)],
        idempotencyKey: Annotated[str, Field(min_length=8, max_length=160)],
        remindAt: Annotated[
            str | None, Field(default=None, description="绝对时间 RFC3339，必须带 UTC offset")
        ] = None,
        localDateTime: Annotated[
            str | None,
            Field(default=None, description="用户所在时区的本地时间 YYYY-MM-DDTHH:MM[:SS]"),
        ] = None,
        afterMinutes: Annotated[
            int | None,
            Field(default=None, ge=1, le=525600, description="从服务器接收时刻起延后分钟数"),
        ] = None,
        timezone: Annotated[
            str | None, Field(default=None, description="IANA 时区；默认账号时区")
        ] = None,
        note: Annotated[str | None, Field(default=None, max_length=2000)] = None,
        scene: Annotated[
            Literal["general", "bookkeeping", "habit"], Field(default="general")
        ] = "general",
        dueAt: Annotated[str | None, Field(default=None, description="可选截止时间")] = None,
    ) -> output.ReminderCreateOutput:
        return cast(
            output.ReminderCreateOutput,
            await env.call(
                lambda ctx: tools.reminder_create(
                    ctx,
                    title=title,
                    note=note,
                    scene=scene,
                    remind_at=remindAt,
                    local_date_time=localDateTime,
                    after_minutes=afterMinutes,
                    deadline_at=dueAt,
                    timezone_name=timezone,
                    idempotency_key=idempotencyKey,
                ),
                tool_name="reminder_create",
                idempotency_key=idempotencyKey,
            ),
        )


def _register_feedback_submit(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=tool_ui_uri("feedback_submit"),
        name="feedback_submit",
        description=_TOOL_DESCRIPTIONS["feedback_submit"],
        title="提交反馈",
    )
    async def feedback_submit(  # pyright: ignore[reportUnusedFunction]
        kind: Annotated[
            Literal["bug", "feature_request", "usability", "question", "other"],
            Field(description="反馈类型"),
        ],
        summary: Annotated[str, Field(min_length=1, max_length=160)],
        details: Annotated[str | None, Field(default=None, max_length=4000)] = None,
        context: Annotated[
            dict | None,
            Field(default=None, description="可选功能名、场景和客户端上下文；不得放入密钥"),
        ] = None,
    ) -> output.FeedbackSubmitOutput:
        return cast(
            output.FeedbackSubmitOutput,
            await env.call(
                lambda ctx: tools.feedback_submit(
                    ctx, kind=kind, summary=summary, details=details, context=context
                ),
                tool_name="feedback_submit",
            ),
        )


def _register_asset_tools(apps: Apps, env: McpToolEnv) -> None:
    @apps.tool(
        resource_uri=tool_ui_uri("asset_upload_intent_create"),
        name="asset_upload_intent_create",
        description=_TOOL_DESCRIPTIONS["asset_upload_intent_create"],
        title="准备上传附件",
    )
    async def asset_upload_intent_create(  # pyright: ignore[reportUnusedFunction]
        contentType: Annotated[str, Field(min_length=3, max_length=120)],
        sizeBytes: Annotated[int, Field(gt=0)],
        idempotencyKey: Annotated[str, Field(min_length=8, max_length=128)],
    ) -> output.AssetUploadIntentOutput:
        return cast(
            output.AssetUploadIntentOutput,
            await env.call(
                lambda ctx: tools.asset_upload_intent_create(
                    ctx,
                    content_type=contentType,
                    size_bytes=sizeBytes,
                    idempotency_key=idempotencyKey,
                ),
                tool_name="asset_upload_intent_create",
                idempotency_key=idempotencyKey,
            ),
        )

    @apps.tool(
        resource_uri=tool_ui_uri("asset_upload_complete"),
        name="asset_upload_complete",
        description=_TOOL_DESCRIPTIONS["asset_upload_complete"],
        title="确认附件上传",
    )
    async def asset_upload_complete(  # pyright: ignore[reportUnusedFunction]
        assetId: Annotated[str, Field(description="上传意图返回的 Asset UUID")],
        idempotencyKey: Annotated[str, Field(min_length=8, max_length=128)],
        checksumSha256: Annotated[
            str | None, Field(default=None, min_length=64, max_length=64)
        ] = None,
    ) -> output.AssetUploadCompleteOutput:
        return cast(
            output.AssetUploadCompleteOutput,
            await env.call(
                lambda ctx: tools.asset_upload_complete(
                    ctx, asset_id=assetId, checksum_sha256=checksumSha256
                ),
                tool_name="asset_upload_complete",
                idempotency_key=idempotencyKey,
            ),
        )


__all__ = [
    "MCP_APP_TOOL_SPECS",
    "build_mcp_server",
    "tool_ui_uri",
]
