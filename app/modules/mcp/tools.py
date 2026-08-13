"""MCP 第一版工具实现（记账读写验证用例）。

业务规则与 REST 一致（走同一批 Repository / 幂等 / 审计规则）：
- `bookkeeping_create`：`moment_types.validate("bookkeeping", payload)`
   + 幂等 + revision 快照 + 审计
- `bookkeeping_list`：复用 moments 列表服务（type 过滤 + 时间范围 + payload 过滤）
- `bookkeeping_summary`：服务端聚合，口径与 Web 记账板块一致（countInFlow + 收支/分类小计）
- `moments_get`：单条查询（Apps UI 详情需要）

工具层不重复实现业务规则；错误通过 `ApplicationError` 抛出，
由 `McpToolEnv.call` 统一映射为 MCP `CallToolResult(isError=True)`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator, FormatChecker
from mcp.types import CallToolResult, TextContent
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.audit_event_repository import (
    SqlAuditEventRepository,
)
from app.infrastructure.database.repositories.habit_goal_repository import (
    SqlHabitGoalRepository,
)
from app.infrastructure.database.repositories.idempotency_repository import (
    SqlIdempotencyRepository,
    fingerprint_payload,
)
from app.infrastructure.database.repositories.moment_repository import (
    PostgresMomentRepository,
)
from app.infrastructure.database.repositories.moment_revision_repository import (
    SqlMomentRevisionRepository,
)
from app.infrastructure.database.repositories.notification_repository import (
    NotificationPipelineRepository,
    ReminderRepository,
)
from app.modules.habit_goals.domain import HabitGoal
from app.modules.mcp.a2ui import A2UI_DISABLED, A2UISupport, build_a2ui_result, build_text_summary
from app.modules.mcp.scope import has_scope
from app.modules.moment_types.registry import validate as validate_moment_type
from app.modules.moments.domain import (
    Moment,
    MomentCategory,
    MomentProvenance,
    ProvenanceSource,
)
from app.modules.notifications.reminders import ReminderService, serialize_reminder

SCHEMA_VERSION = "1.0"

# bookkeeping payload 中参与幂等指纹的字段（与 REST create 的 body 语义对齐）
_IDEMPOTENT_FIELDS = (
    "title",
    "amount",
    "flow",
    "occurredAt",
    "account",
    "category",
    "merchant",
    "ledger",
    "method",
    "countInFlow",
    "countInBudget",
)


@dataclass(slots=True)
class McpCallContext:
    """一次 MCP 工具调用的执行上下文。"""

    user_id: UUID
    scopes: tuple[str, ...]
    method: str
    actor_id: str | None
    request_id: str
    session: AsyncSession
    account_timezone: str | None = None
    a2ui: A2UISupport = A2UI_DISABLED
    available_tools: frozenset[str] | None = None

    def require_scope(self, required: str) -> None:
        if not has_scope(self.scopes, required):
            raise ApplicationError(
                code="SCOPE_DENIED",
                message=f"Token 缺少 {required} 权限，无法执行该工具。",
                status_code=403,
                details={"requiredScope": required, "scopes": list(self.scopes)},
            )


# ---------------------------------------------------------------------------
# 输出组装（structuredContent + 文本降级双通道）
# ---------------------------------------------------------------------------


def _text_result(data: dict) -> CallToolResult:
    """成功结果：structuredContent + 可读 JSON 文本（降级通道）。"""
    import json

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))],
        structured_content=data,
    )


def _tool_result(ctx: McpCallContext, tool_name: str, data: dict) -> CallToolResult:
    return build_a2ui_result(
        support=ctx.a2ui,
        tool_name=tool_name,
        request_id=ctx.request_id,
        structured_content=data,
        text=build_text_summary(tool_name, data),
    )


def err_result(code: str, message: str, details: dict | None = None) -> CallToolResult:
    """错误结果（契约稳定错误码，见 docs/contracts/MCP_SERVER.md §7）。"""
    return CallToolResult(
        content=[TextContent(type="text", text=f"{code}: {message}")],
        is_error=True,
        structured_content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def _bookkeeping_payload_from_args(
    *,
    amount: float,
    flow: str,
    account: str | None,
    category: str | None,
    merchant: str | None,
    ledger: str | None,
    method: str | None,
    count_in_flow: bool | None,
    count_in_budget: bool | None,
) -> dict:
    payload: dict = {"amount": amount, "flow": flow}
    if account is not None:
        payload["account"] = account
    if category is not None:
        payload["category"] = category
    if merchant is not None:
        payload["merchant"] = merchant
    if ledger is not None:
        payload["ledger"] = ledger
    if method is not None:
        payload["method"] = method
    if count_in_flow is not None:
        payload["countInFlow"] = count_in_flow
    if count_in_budget is not None:
        payload["countInBudget"] = count_in_budget
    return payload


def resolve_occurred_time(
    value: str | None,
    local_date_time: str | None,
    timezone_name: str,
    reference_time: datetime | None = None,
) -> tuple[datetime, str]:
    if value is not None and local_date_time is not None:
        raise ApplicationError(
            code="OCCURRED_TIME_INPUT_INVALID",
            message="occurredAt 与 occurredLocalDateTime 只能提供一个。",
            status_code=400,
        )
    if local_date_time is not None:
        return _local_datetime(local_date_time, timezone_name, "occurredLocalDateTime"), "local"
    if value is None:
        return reference_time or datetime.now(UTC), "server"
    try:
        resolved = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if resolved.tzinfo is None:
            raise ValueError("occurredAt must include an offset")
        return resolved, "absolute"
    except ValueError as exc:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="occurredAt 必须是带时区 offset 的 RFC3339 时间。",
            status_code=400,
            details={"occurredAt": value},
        ) from exc


async def _resolve_timezone(ctx: McpCallContext, value: str | None) -> str:
    """Use an explicit IANA zone, then the account preference, finally UTC."""
    resolved = value or ctx.account_timezone or "UTC"
    _parse_timezone(resolved)
    return resolved


def _local_datetime(value: str, timezone_name: str, field: str) -> datetime:
    """Resolve a wall-clock value and reject DST gaps/overlaps instead of guessing."""
    try:
        local = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message=f"{field} 必须是 YYYY-MM-DDTHH:MM[:SS] 本地时间。",
            status_code=400,
            details={field: value},
        ) from exc
    if local.tzinfo is not None:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message=f"{field} 不应包含 UTC offset；请通过 timezone 指定 IANA 时区。",
            status_code=400,
        )
    zone = _parse_timezone(timezone_name)
    first = local.replace(tzinfo=zone, fold=0)
    second = local.replace(tzinfo=zone, fold=1)
    first_back = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    second_back = second.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    valid_first = first_back == local
    valid_second = second_back == local
    if not valid_first and not valid_second:
        raise ApplicationError(
            code="LOCAL_TIME_NONEXISTENT",
            message="该本地时间处于夏令时跳变空档，请选择其他时间。",
            status_code=400,
            details={field: value, "timezone": timezone_name},
        )
    if valid_first and valid_second and first.utcoffset() != second.utcoffset():
        raise ApplicationError(
            code="LOCAL_TIME_AMBIGUOUS",
            message="该本地时间因夏令时切换出现两次，请改用带 offset 的 remindAt。",
            status_code=400,
            details={field: value, "timezone": timezone_name},
        )
    return first if valid_first else second


def resolve_reminder_time(
    *,
    remind_at: str | None,
    local_date_time: str | None,
    after_minutes: int | None,
    timezone_name: str,
    reference_time: datetime | None = None,
) -> tuple[datetime, str]:
    supplied = sum(value is not None for value in (remind_at, local_date_time, after_minutes))
    if supplied != 1:
        raise ApplicationError(
            code="REMINDER_TIME_INPUT_INVALID",
            message="remindAt、localDateTime、afterMinutes 必须且只能提供一个。",
            status_code=400,
        )
    try:
        if remind_at is not None:
            resolved = datetime.fromisoformat(remind_at.replace("Z", "+00:00"))
            if resolved.tzinfo is None:
                raise ValueError("remindAt must include an offset")
            return resolved, "absolute"
        if local_date_time is not None:
            return _local_datetime(local_date_time, timezone_name, "localDateTime"), "local"
        return (reference_time or datetime.now(UTC)) + timedelta(
            minutes=after_minutes or 0
        ), "relative"
    except ValueError as exc:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="remindAt 必须是带时区 offset 的 RFC3339 时间。",
            status_code=400,
        ) from exc


def _bookkeeping_item(moment: Moment) -> dict:
    p = moment.payload or {}
    return {
        "id": str(moment.id),
        "title": moment.title,
        "amount": p.get("amount", 0),
        "flow": p.get("flow", "expense"),
        "currency": p.get("currency", "CNY"),
        "category": p.get("category"),
        "merchant": p.get("merchant"),
        "ledger": p.get("ledger"),
        "account": p.get("account"),
        "occurredAt": moment.occurred_at.isoformat(),
        "revision": moment.revision,
    }


def _moment_item(moment: Moment, *, detail: bool = False) -> dict:
    """MCP Apps 使用的稳定 Moment 摘要；detail=True 时补充正文与来源信息。"""
    item: dict = {
        "id": str(moment.id),
        "title": moment.title,
        "description": moment.description,
        "category": moment.category.value,
        "type": moment.moment_type,
        "tags": list(moment.tags),
        "persons": list(moment.persons),
        "event": moment.event,
        "occurredAt": moment.occurred_at.isoformat(),
        "timezone": moment.timezone,
        "revision": moment.revision,
    }
    if moment.moment_type != "general" or detail:
        item["payload"] = moment.payload
    if detail:
        item.update(
            {
                "voiceInput": moment.voice_input,
                "aiSummary": moment.ai_summary,
                "location": (
                    {
                        "name": moment.location.name,
                        "latitude": moment.location.latitude,
                        "longitude": moment.location.longitude,
                        "source": moment.location.source.value,
                    }
                    if moment.location
                    else None
                ),
                "emotion": (
                    {
                        "label": moment.emotion.label,
                        "valence": moment.emotion.valence,
                        "arousal": moment.emotion.arousal,
                    }
                    if moment.emotion
                    else None
                ),
                "provenance": moment.provenance.to_dict() if moment.provenance else None,
                "createdAt": moment.created_at.isoformat(),
                "updatedAt": moment.updated_at.isoformat(),
            }
        )
    return item


def _parse_timezone(value: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(value or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="timezone 不是合法的 IANA 时区。",
            status_code=400,
            details={"timezone": value},
        ) from exc


def _day_bounds(day: str | None, timezone_name: str | None) -> tuple[datetime, datetime, date]:
    zone = _parse_timezone(timezone_name)
    if day:
        try:
            local_day = date.fromisoformat(day)
        except ValueError as exc:
            raise ApplicationError(
                code="INVALID_ARGUMENTS",
                message="date 必须是 YYYY-MM-DD。",
                status_code=400,
                details={"date": day},
            ) from exc
    else:
        local_day = datetime.now(zone).date()
    start = datetime.combine(local_day, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return start, end, local_day


async def _append_tool_audit(
    ctx: McpCallContext,
    *,
    tool: str,
    result_count: int | None = None,
    resource_id: UUID | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    details: dict[str, object] = {"tool": tool, "scopes": list(ctx.scopes)}
    if result_count is not None:
        details["resultCount"] = result_count
    if metadata:
        details.update(metadata)
    await SqlAuditEventRepository(ctx.session).append(
        user_id=ctx.user_id,
        actor_type="mcp",
        actor_id=ctx.actor_id,
        event_type=f"mcp.tool.{tool}",
        resource_type="moment",
        resource_id=resource_id,
        request_id=ctx.request_id,
        allowed=True,
        metadata=details,
    )


# ---------------------------------------------------------------------------
# 工具：bookkeeping_create
# ---------------------------------------------------------------------------


async def bookkeeping_create(
    ctx: McpCallContext,
    *,
    amount: float,
    flow: str,
    occurred_at: str | None,
    occurred_local_date_time: str | None,
    timezone_name: str | None,
    account: str | None,
    category: str | None,
    merchant: str | None,
    ledger: str | None,
    method: str | None,
    count_in_flow: bool | None,
    count_in_budget: bool | None,
    idempotency_key: str | None,
    title: str | None,
) -> CallToolResult:
    ctx.require_scope("moments.write")

    payload = _bookkeeping_payload_from_args(
        amount=amount,
        flow=flow,
        account=account,
        category=category,
        merchant=merchant,
        ledger=ledger,
        method=method,
        count_in_flow=count_in_flow,
        count_in_budget=count_in_budget,
    )
    # 类型校验（复用注册表，非法 payload → INVALID_ARGUMENTS）
    validate_moment_type("bookkeeping", payload)

    resolved_title = (title or category or merchant or "记账").strip()[:20]
    if not resolved_title:
        resolved_title = "记账"

    resolved_timezone = await _resolve_timezone(ctx, timezone_name)
    occurred, occurred_source = resolve_occurred_time(
        occurred_at, occurred_local_date_time, resolved_timezone
    )

    # 幂等：传 idempotencyKey 时启用（与 REST 一致）
    idem_repo: SqlIdempotencyRepository | None = None
    idem_record = None
    if idempotency_key:
        idem_repo = SqlIdempotencyRepository(ctx.session)
        request_body = {
            k: v
            for k, v in {
                "title": resolved_title,
                "amount": amount,
                "flow": flow,
                "occurredAt": occurred_at,
                "occurredLocalDateTime": occurred_local_date_time,
                "timezone": resolved_timezone,
                "account": account,
                "category": category,
                "merchant": merchant,
                "ledger": ledger,
                "method": method,
                "countInFlow": count_in_flow,
                "countInBudget": count_in_budget,
            }.items()
            if v is not None
        }
        idem_record = await idem_repo.acquire(
            user_id=ctx.user_id,
            operation="bookkeeping_create",
            idempotency_key=idempotency_key,
            request_payload=request_body,
        )
        if idem_record.request_fingerprint != fingerprint_payload(request_body):
            raise ApplicationError(
                code="IDEMPOTENCY_CONFLICT",
                message="idempotencyKey 已用于不同的请求体。",
                status_code=409,
            )
        if idem_record.state == "completed" and idem_record.response_body is not None:
            replay_response = {
                **idem_record.response_body,
                "created": False,
                "replayed": True,
            }
            return _tool_result(ctx, "bookkeeping_create", replay_response)

    moment = Moment(
        id=uuid4(),
        user_id=ctx.user_id,
        title=resolved_title,
        description=None,
        voice_input=None,
        ai_summary=None,
        category=MomentCategory.EXPERIENCE,
        tags=(),
        persons=(),
        event=None,
        occurred_at=occurred,
        timezone=resolved_timezone,
        revision=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        provenance=MomentProvenance(
            source=ProvenanceSource.MCP,
            client_id=ctx.actor_id,
            mcp_tool_name="bookkeeping_create",
        ),
        moment_type="bookkeeping",
        payload=payload,
    )

    repo = PostgresMomentRepository(ctx.session)
    created = await repo.create(moment)

    response = {
        "schemaVersion": SCHEMA_VERSION,
        "id": str(created.id),
        "title": created.title,
        "amount": payload["amount"],
        "flow": payload["flow"],
        "currency": payload.get("currency", "CNY"),
        "category": payload.get("category"),
        "ledger": payload.get("ledger"),
        "occurredAt": created.occurred_at.isoformat(),
        "occurredTimeSource": occurred_source,
        "revision": created.revision,
        "created": True,
        "replayed": False,
    }

    # 版本快照
    revision_repo = SqlMomentRevisionRepository(ctx.session)
    await revision_repo.append(
        user_id=created.user_id,
        moment_id=created.id,
        revision=created.revision,
        operation="created",
        snapshot=response,
        actor_user_id=ctx.user_id,
    )

    # 审计（actorType=mcp）
    audit_repo = SqlAuditEventRepository(ctx.session)
    await audit_repo.append(
        user_id=ctx.user_id,
        actor_type="mcp",
        actor_id=ctx.actor_id,
        event_type="mcp.tool.bookkeeping_create",
        resource_type="moment",
        resource_id=created.id,
        request_id=ctx.request_id,
        allowed=True,
        metadata={
            "tool": "bookkeeping_create",
            "scopes": list(ctx.scopes),
            "amount": payload["amount"],
            "flow": payload["flow"],
            "idempotencyKey": bool(idempotency_key),
        },
    )

    if idem_repo is not None and idem_record is not None:
        await idem_repo.complete(
            record_id=idem_record.id,
            response_status=201,
            response_body=response,
            resource_id=created.id,
        )

    return _tool_result(ctx, "bookkeeping_create", response)


# ---------------------------------------------------------------------------
# 工具：bookkeeping_list
# ---------------------------------------------------------------------------


async def bookkeeping_list(
    ctx: McpCallContext,
    *,
    limit: int,
    cursor: str | None,
    from_: str | None,
    to: str | None,
    category: str | None,
    ledger: str | None,
) -> CallToolResult:
    ctx.require_scope("moments.read")

    repo = PostgresMomentRepository(ctx.session)
    payload_eq = {"ledger": ledger} if ledger else None
    moments, has_more, next_cursor = await repo.list_by_user(
        user_id=ctx.user_id,
        limit=limit,
        cursor=cursor,
        moment_type="bookkeeping",
        category=category,
        occurred_from=_parse_optional_datetime(from_, "from") if from_ else None,
        occurred_to=_parse_optional_datetime(to, "to") if to else None,
        payload_eq=payload_eq,
    )

    items = [_bookkeeping_item(m) for m in moments]
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "total": len(items),
        "items": items,
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }

    # 审计（读操作）
    audit_repo = SqlAuditEventRepository(ctx.session)
    await audit_repo.append(
        user_id=ctx.user_id,
        actor_type="mcp",
        actor_id=ctx.actor_id,
        event_type="mcp.tool.bookkeeping_list",
        resource_type="moment",
        request_id=ctx.request_id,
        allowed=True,
        metadata={
            "tool": "bookkeeping_list",
            "scopes": list(ctx.scopes),
            "resultCount": len(items),
            "hasMore": has_more,
        },
    )

    return _tool_result(ctx, "bookkeeping_list", result)


# ---------------------------------------------------------------------------
# 工具：bookkeeping_summary
# ---------------------------------------------------------------------------


def _period_bounds(
    period: str,
    year: int | None,
    month: int | None,
) -> tuple[datetime, datetime, dict]:
    now = datetime.now(UTC)
    y = year or now.year
    if period == "month":
        m = month or now.month
        if not 1 <= m <= 12:
            raise ApplicationError(
                code="INVALID_ARGUMENTS",
                message="month 必须在 1~12 之间。",
                status_code=400,
                details={"month": m},
            )
        start = datetime(y, m, 1, tzinfo=UTC)
        end = datetime(y + 1, 1, 1, tzinfo=UTC) if m == 12 else datetime(y, m + 1, 1, tzinfo=UTC)
        label = {"period": "month", "year": y, "month": m}
    elif period == "quarter":
        q = month or ((now.month - 1) // 3 + 1)
        if not 1 <= q <= 4:
            raise ApplicationError(
                code="INVALID_ARGUMENTS",
                message="quarter 必须在 1~4 之间。",
                status_code=400,
                details={"quarter": q},
            )
        start = datetime(y, (q - 1) * 3 + 1, 1, tzinfo=UTC)
        end = datetime(y + 1, 1, 1, tzinfo=UTC) if q == 4 else datetime(y, q * 3 + 1, 1, tzinfo=UTC)
        label = {"period": "quarter", "year": y, "quarter": q}
    else:  # year
        start = datetime(y, 1, 1, tzinfo=UTC)
        end = datetime(y + 1, 1, 1, tzinfo=UTC)
        label = {"period": "year", "year": y}
    return start, end, label


async def bookkeeping_summary(
    ctx: McpCallContext,
    *,
    period: str,
    year: int | None,
    month: int | None,
    ledger: str | None,
    category: str | None,
    from_: str | None = None,
    to: str | None = None,
) -> CallToolResult:
    ctx.require_scope("moments.read")

    # 自定义范围（plan 对「今天/昨天」解析为精确 from/to）：范围优先于 period
    if from_ or to:
        start = _parse_optional_datetime(from_, "from") if from_ else None
        end = _parse_optional_datetime(to, "to") if to else None
        label = {
            "period": "custom",
            "from": (start or datetime.min.replace(tzinfo=UTC)).isoformat(),
            "to": (end or datetime.max.replace(tzinfo=UTC)).isoformat(),
        }
        custom = True
    else:
        if period not in ("month", "quarter", "year"):
            raise ApplicationError(
                code="INVALID_ARGUMENTS",
                message="period 必须是 month / quarter / year。",
                status_code=400,
                details={"period": period},
            )
        start, end, label = _period_bounds(period, year, month)
        custom = False

    repo = PostgresMomentRepository(ctx.session)
    payload_eq: dict[str, str] = {}
    if ledger:
        payload_eq["ledger"] = ledger
    if category:
        payload_eq["category"] = category
    moments = await repo.list_by_user_range(
        user_id=ctx.user_id,
        occurred_from=start,
        occurred_to=end,
        moment_type="bookkeeping",
        payload_eq=payload_eq or None,
    )

    # 口径与 Web 记账板块一致（bookkeeping-stats.tsx）：
    # - countInFlow !== false 才参与统计
    # - income 累加收入，其余累加支出；balance = income - expense
    # - 分类小计只看支出，空分类归入「未分类」
    income = 0.0
    expense = 0.0
    count = 0
    by_category: dict[str, float] = {}
    for m in moments:
        p = m.payload or {}
        if p.get("countInFlow") is False:
            continue
        amount = p.get("amount", 0)
        if not isinstance(amount, (int, float)):
            continue
        count += 1
        if p.get("flow") == "income":
            income += amount
        else:
            expense += amount
            cat = (p.get("category") or "未分类").strip() or "未分类"
            by_category[cat] = by_category.get(cat, 0.0) + amount

    category_share = [
        {"category": name, "amount": round(amount, 2)}
        for name, amount in sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)
    ]

    result = {
        "schemaVersion": SCHEMA_VERSION,
        **label,
        "income": round(income, 2),
        "expense": round(expense, 2),
        "balance": round(income - expense, 2),
        "count": count,
        "byCategory": category_share,
        "from": (start.isoformat() if start else label.get("from")),
        "to": (end.isoformat() if end else label.get("to")),
    }

    audit_repo = SqlAuditEventRepository(ctx.session)
    await audit_repo.append(
        user_id=ctx.user_id,
        actor_type="mcp",
        actor_id=ctx.actor_id,
        event_type="mcp.tool.bookkeeping_summary",
        resource_type="moment",
        request_id=ctx.request_id,
        allowed=True,
        metadata={
            "tool": "bookkeeping_summary",
            "scopes": list(ctx.scopes),
            "period": "custom" if custom else period,
            "resultCount": count,
        },
    )

    return _tool_result(ctx, "bookkeeping_summary", result)


# ---------------------------------------------------------------------------
# 工具：bookkeeping_plan（记账意图确定性解析，供眼镜端预路由）
# ---------------------------------------------------------------------------

# 常见分类关键词（与 Web 记账口径一致的常见分类）
_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("餐饮", ("餐", "饭", "吃", "喝", "咖啡", "奶茶", "外卖")),
    ("交通", ("打车", "出租车", "地铁", "公交", "高铁", "机票", "加油", "停车")),
    ("购物", ("买", "购物", "淘宝", "京东", "衣服", "鞋", "超市")),
    ("娱乐", ("电影", "游戏", "演唱会", "KTV", "球赛")),
    ("居住", ("房租", "水电", "物业", "燃气")),
    ("医疗", ("药", "医院", "挂号", "体检")),
]


def _resolve_period(text: str, now: datetime) -> dict:
    """时间词 → (period, year, month) 或 (period=custom, from, to)。

    支持今天/昨天（精确到日的范围统计）、上月/上季度/今年/去年/某年/某月。
    """
    # 今天/昨天：按 UTC 日界（Server 无用户时区，与存储口径一致；
    # 未来可加 timezone 参数按用户时区切日界）
    if re.search(r"今天|今日|当天", text):
        start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        return {
            "period": "custom",
            "from": start.isoformat(),
            "to": (start + timedelta(days=1)).isoformat(),
        }
    if re.search(r"昨天|昨日", text):
        day = now - timedelta(days=1)
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return {
            "period": "custom",
            "from": start.isoformat(),
            "to": (start + timedelta(days=1)).isoformat(),
        }
    year = now.year
    month = now.month
    if re.search(r"去年|上年|上一年", text):
        return {"period": "year", "year": year - 1, "month": None}
    if re.search(r"今年|本年", text):
        return {"period": "year", "year": year, "month": None}
    if re.search(r"上季度|上季|上个季度", text):
        q = (now.month - 1) // 3
        prev_q = q - 1 if q > 0 else 3
        prev_year = year if q > 0 else year - 1
        return {"period": "quarter", "year": prev_year, "month": prev_q}
    if re.search(r"本季度|本季|这个季度", text):
        return {"period": "quarter", "year": year, "month": (now.month - 1) // 3 + 1}
    if re.search(r"上个月|上月|前一个月", text):
        m = month - 1 if month > 1 else 12
        y = year if month > 1 else year - 1
        return {"period": "month", "year": y, "month": m}
    if re.search(r"这个月|本月|这个月", text):
        return {"period": "month", "year": year, "month": month}
    ym = re.search(r"(\d{4})年(\d{1,2})月", text)
    if ym:
        return {"period": "month", "year": int(ym.group(1)), "month": int(ym.group(2))}
    m = re.search(r"(\d{1,2})月", text)
    if m:
        return {"period": "month", "year": year, "month": int(m.group(1))}
    y = re.search(r"(\d{4})年", text)
    if y:
        return {"period": "year", "year": int(y.group(1)), "month": None}
    return {"period": "month", "year": year, "month": month}


def _resolve_create(text: str, now: datetime) -> dict | None:
    """记一笔解析：金额/流向/分类/时间。无法识别 → None。"""
    amount_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|毛)?", text)
    if not amount_match:
        return None
    amount = round(float(amount_match.group(1)), 2)
    flow = "income" if re.search(r"收入|赚|进账|入账", text) else "expense"
    category = None
    for name, keywords in _CATEGORY_KEYWORDS:
        if any(k in text for k in keywords):
            category = name
            break
    # 发生时间：昨天 → now-1d；带“早上/中午/晚上”保持当天；其余用 now
    occurred_at = now.isoformat()
    if re.search(r"昨天", text):
        occurred_at = (now - timedelta(days=1)).isoformat()
    return {
        "amount": amount,
        "flow": flow,
        "category": category,
        "occurredAt": occurred_at,
        "idempotencyKey": str(uuid4()),
    }


async def bookkeeping_plan(
    ctx: McpCallContext,
    *,
    input: str,
) -> CallToolResult:
    """记账意图确定性解析（供眼镜端预路由，工具/解析逻辑均在远程）。

    输入用户原话，输出结构化计划：
    - action=summary → 用 args(period/year/month) 调 bookkeeping_summary
    - action=create → 用 args(amount/flow/category/occurredAt/idempotencyKey) 调 bookkeeping_create
    - action=list → 用 args(limit/from/to) 调 bookkeeping_list
    - action=none → reply 提示话术（眼镜端可降级给 LLM）
    """
    ctx.require_scope("moments.read")
    text = str(input or "").strip()
    now = datetime.now(UTC)

    if not text:
        return _text_result(
            {"action": "none", "args": {}, "reply": "没有听清要做什么，请再说一遍。"}
        )

    # 记账（写）：记一笔/记账/花了 xx/消费 xx/支出 xx/收入 xx
    looks_create = bool(
        re.search(r"记(?:一笔|一下|个)|记账|入账", text)
        or re.search(r"(花了|消费|支出|花了|用掉|收入|赚了)\s*\d", text)
    )
    if looks_create:
        create_args = _resolve_create(text, now)
        if create_args is not None:
            return _text_result({"action": "create", "args": create_args})

    # 查统计：花了多少/收支/结余/开销/账单统计
    looks_summary = bool(
        re.search(
            r"花了多少|用了多少|开销多少|收支|结余|账单统计|支出.*(多少|统计|汇总)|收入.*(多少|统计|汇总)",
            text,
        )
    )
    if looks_summary:
        period_info = _resolve_period(text, now)
        args: dict = {"period": period_info["period"]}
        if period_info.get("from") is not None:
            # SDK 参数键名：Python 参数 from_ 暴露为 "from_"
            args["from_"] = period_info["from"]
            args["to"] = period_info["to"]
        if period_info.get("year") is not None:
            args["year"] = period_info["year"]
        if period_info.get("month") is not None:
            args["month"] = period_info["month"]
        return _text_result({"action": "summary", "args": args})

    # 查明细：账单/明细/列表/消费记录
    looks_list = bool(re.search(r"明细|账单|列表|消费记录|订单记录|流水", text))
    if looks_list:
        return _text_result({"action": "list", "args": {"limit": 20}})

    return _text_result(
        {
            "action": "none",
            "args": {},
            "reply": "我没听清要查哪笔账。可以试试「上个月花了多少」「3月账单」"
            "或「记一笔午餐 28.5 元」。",
        }
    )


# ---------------------------------------------------------------------------
# 工具：moments_get
# ---------------------------------------------------------------------------


async def moments_get(
    ctx: McpCallContext,
    *,
    moment_id: str,
) -> CallToolResult:
    ctx.require_scope("moments.read")

    try:
        mid = UUID(moment_id)
    except (ValueError, TypeError) as exc:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="momentId 不是合法的 UUID。",
            status_code=400,
            details={"momentId": moment_id},
        ) from exc

    repo = PostgresMomentRepository(ctx.session)
    moment = await repo.get_by_id(mid, ctx.user_id)
    if moment is None:
        raise ApplicationError(
            code="MOMENT_NOT_FOUND",
            message="未找到该 Moment。",
            status_code=404,
            details={"momentId": moment_id},
        )

    result = {
        "schemaVersion": SCHEMA_VERSION,
        "id": str(moment.id),
        "title": moment.title,
        "description": moment.description,
        "category": moment.category.value,
        "type": moment.moment_type,
        "payload": moment.payload,
        "occurredAt": moment.occurred_at.isoformat(),
        "revision": moment.revision,
        "provenance": moment.provenance.to_dict() if moment.provenance else None,
    }

    audit_repo = SqlAuditEventRepository(ctx.session)
    await audit_repo.append(
        user_id=ctx.user_id,
        actor_type="mcp",
        actor_id=ctx.actor_id,
        event_type="mcp.tool.moments_get",
        resource_type="moment",
        resource_id=mid,
        request_id=ctx.request_id,
        allowed=True,
        metadata={"tool": "moments_get", "scopes": list(ctx.scopes)},
    )

    return _tool_result(ctx, "moments_get", result)


def _parse_optional_datetime(value: str, name: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message=f"{name} 不是合法的 ISO-8601 时间。",
            status_code=400,
            details={name: value},
        ) from exc


# ---------------------------------------------------------------------------
# 通用 Moment 工具：创建 / 时间线 / 搜索 / 统计 / 每日回顾
# ---------------------------------------------------------------------------


async def _create_typed_moment(
    ctx: McpCallContext,
    *,
    title: str,
    description: str | None,
    category: str,
    tags: list[str] | None,
    persons: list[str] | None,
    event: str | None,
    occurred_at: str | None,
    occurred_local_date_time: str | None,
    timezone_name: str | None,
    moment_type: str,
    payload: dict | None,
    idempotency_key: str,
    operation: str,
) -> tuple[Moment, bool]:
    resolved_title = title.strip()
    if not resolved_title or len(resolved_title) > 20:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="title 必须为 1~20 个字符。",
            status_code=400,
        )
    if description is not None and len(description) > 240:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="description 不能超过 240 个字符。",
            status_code=400,
        )
    try:
        resolved_category = MomentCategory(category)
    except ValueError as exc:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="category 不在允许范围内。",
            status_code=400,
            details={"category": category},
        ) from exc
    if tags and (len(tags) > 5 or any(len(tag) > 20 for tag in tags)):
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="tags 最多 5 个且每项不超过 20 个字符。",
            status_code=400,
        )
    if persons and (len(persons) > 10 or any(len(person) > 20 for person in persons)):
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="persons 最多 10 个且每项不超过 20 个字符。",
            status_code=400,
        )
    if event is not None and len(event) > 50:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="event 不能超过 50 个字符。",
            status_code=400,
        )
    timezone_name = await _resolve_timezone(ctx, timezone_name)
    resolved_payload = payload or {}
    validate_moment_type(moment_type, resolved_payload)
    occurred, _ = resolve_occurred_time(occurred_at, occurred_local_date_time, timezone_name)

    request_body = {
        "title": resolved_title,
        "description": description,
        "category": resolved_category.value,
        "tags": tags or [],
        "persons": persons or [],
        "event": event,
        "occurredAt": occurred_at,
        "occurredLocalDateTime": occurred_local_date_time,
        "timezone": timezone_name,
        "type": moment_type,
        "payload": resolved_payload,
    }
    idem_repo = SqlIdempotencyRepository(ctx.session)
    request_fp = fingerprint_payload(request_body)
    idem_record = await idem_repo.acquire(
        user_id=ctx.user_id,
        operation=operation,
        idempotency_key=idempotency_key,
        request_payload=request_body,
    )
    if idem_record.request_fingerprint != request_fp:
        raise ApplicationError(
            code="IDEMPOTENCY_CONFLICT",
            message="idempotencyKey 已用于不同的请求参数。",
            status_code=409,
        )
    if idem_record.state == "completed" and idem_record.response_body:
        resource_id = idem_record.resource_id
        if resource_id:
            existing = await PostgresMomentRepository(ctx.session).get_by_id(
                resource_id, ctx.user_id
            )
            if existing is not None:
                return existing, True

    now = datetime.now(UTC)
    moment = Moment(
        id=uuid4(),
        user_id=ctx.user_id,
        title=resolved_title,
        description=description,
        voice_input=None,
        ai_summary=None,
        category=resolved_category,
        tags=tuple(dict.fromkeys(tags or [])),
        persons=tuple(dict.fromkeys(persons or [])),
        event=event,
        occurred_at=occurred,
        timezone=timezone_name,
        revision=1,
        created_at=now,
        updated_at=now,
        provenance=MomentProvenance(
            source=ProvenanceSource.MCP,
            client_id=ctx.actor_id,
            mcp_tool_name=operation,
        ),
        moment_type=moment_type,
        payload=resolved_payload,
    )
    created = await PostgresMomentRepository(ctx.session).create(moment)
    response = _moment_item(created, detail=True)
    await SqlMomentRevisionRepository(ctx.session).append(
        user_id=created.user_id,
        moment_id=created.id,
        revision=created.revision,
        operation="created",
        snapshot=response,
        actor_user_id=ctx.user_id,
    )
    await idem_repo.complete(
        record_id=idem_record.id,
        response_status=201,
        response_body=response,
        resource_id=created.id,
    )
    return created, False


async def moments_create(
    ctx: McpCallContext,
    *,
    title: str,
    description: str | None,
    category: str,
    tags: list[str] | None,
    persons: list[str] | None,
    event: str | None,
    occurred_at: str | None,
    occurred_local_date_time: str | None,
    timezone_name: str | None,
    moment_type: str,
    payload: dict | None,
    idempotency_key: str,
) -> CallToolResult:
    ctx.require_scope("moments.write")
    created, replayed = await _create_typed_moment(
        ctx,
        title=title,
        description=description,
        category=category,
        tags=tags,
        persons=persons,
        event=event,
        occurred_at=occurred_at,
        occurred_local_date_time=occurred_local_date_time,
        timezone_name=timezone_name,
        moment_type=moment_type,
        payload=payload,
        idempotency_key=idempotency_key,
        operation="moments_create",
    )
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "created": not replayed,
        "replayed": replayed,
        "moment": _moment_item(created, detail=True),
    }
    await _append_tool_audit(
        ctx,
        tool="moments_create",
        resource_id=created.id,
        metadata={"replayed": replayed, "type": created.moment_type},
    )
    return _text_result(result)


async def moments_list(
    ctx: McpCallContext,
    *,
    limit: int,
    cursor: str | None,
    moment_type: str | None,
    category: str | None,
    tag: str | None,
    from_: str | None,
    to: str | None,
) -> CallToolResult:
    ctx.require_scope("moments.read")
    start = _parse_optional_datetime(from_, "from") if from_ else None
    end = _parse_optional_datetime(to, "to") if to else None
    moments, has_more, next_cursor = await PostgresMomentRepository(ctx.session).list_by_user(
        ctx.user_id,
        limit=limit,
        cursor=cursor,
        moment_type=moment_type,
        category=category,
        tag=tag,
        occurred_from=start,
        occurred_to=end,
    )
    items = [_moment_item(moment) for moment in moments]
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "view": "timeline",
        "items": items,
        "total": len(items),
        "hasMore": has_more,
        "nextCursor": next_cursor,
        "filters": {
            "type": moment_type,
            "category": category,
            "tag": tag,
            "from": from_,
            "to": to,
        },
    }
    await _append_tool_audit(ctx, tool="moments_list", result_count=len(items))
    return _tool_result(ctx, "moments_list", result)


async def moments_search(
    ctx: McpCallContext,
    *,
    query: str,
    limit: int,
    moment_type: str | None,
    category: str | None,
    from_: str | None,
    to: str | None,
) -> CallToolResult:
    ctx.require_scope("moments.read")
    normalized = query.strip().casefold()
    if not normalized:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="query 不能为空。",
            status_code=400,
        )
    start = _parse_optional_datetime(from_, "from") if from_ else None
    end = _parse_optional_datetime(to, "to") if to else None
    moments = await PostgresMomentRepository(ctx.session).list_by_user_range(
        ctx.user_id,
        occurred_from=start,
        occurred_to=end,
        moment_type=moment_type,
    )
    matched: list[Moment] = []
    for moment in moments:
        if category and moment.category.value != category:
            continue
        searchable = " ".join(
            [
                moment.title,
                moment.description or "",
                moment.ai_summary or "",
                " ".join(moment.tags),
                " ".join(moment.persons),
                moment.event or "",
                " ".join(str(value) for value in (moment.payload or {}).values()),
            ]
        ).casefold()
        if normalized in searchable:
            matched.append(moment)
    matched.sort(key=lambda item: (item.occurred_at, item.id), reverse=True)
    visible = matched[:limit]
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "view": "search",
        "query": query,
        "items": [_moment_item(moment) for moment in visible],
        "total": len(matched),
        "hasMore": len(matched) > limit,
        "filters": {
            "type": moment_type,
            "category": category,
            "from": from_,
            "to": to,
        },
    }
    await _append_tool_audit(
        ctx,
        tool="moments_search",
        result_count=len(visible),
        metadata={"queryLength": len(query)},
    )
    return _tool_result(ctx, "moments_search", result)


async def moments_count(
    ctx: McpCallContext,
    *,
    moment_type: str | None,
    category: str | None,
    from_: str | None,
    to: str | None,
) -> CallToolResult:
    ctx.require_scope("moments.read")
    start = _parse_optional_datetime(from_, "from") if from_ else None
    end = _parse_optional_datetime(to, "to") if to else None
    moments = await PostgresMomentRepository(ctx.session).list_by_user_range(
        ctx.user_id,
        occurred_from=start,
        occurred_to=end,
        moment_type=moment_type,
    )
    if category:
        moments = [moment for moment in moments if moment.category.value == category]
    by_category: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for moment in moments:
        by_category[moment.category.value] = by_category.get(moment.category.value, 0) + 1
        by_type[moment.moment_type] = by_type.get(moment.moment_type, 0) + 1
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "count": len(moments),
        "byCategory": by_category,
        "byType": by_type,
        "from": from_,
        "to": to,
    }
    await _append_tool_audit(ctx, tool="moments_count", result_count=len(moments))
    return _text_result(result)


async def reviews_daily(
    ctx: McpCallContext,
    *,
    day: str | None,
    timezone_name: str | None,
) -> CallToolResult:
    ctx.require_scope("moments.read")
    start, end, local_day = _day_bounds(day, timezone_name)
    moments = await PostgresMomentRepository(ctx.session).list_by_user_range(
        ctx.user_id,
        occurred_from=start,
        occurred_to=end,
    )
    moments.sort(key=lambda item: (item.occurred_at, item.id), reverse=True)
    by_category: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for moment in moments:
        by_category[moment.category.value] = by_category.get(moment.category.value, 0) + 1
        by_type[moment.moment_type] = by_type.get(moment.moment_type, 0) + 1
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "view": "daily-review",
        "date": local_day.isoformat(),
        "timezone": timezone_name or "UTC",
        "count": len(moments),
        "byCategory": by_category,
        "byType": by_type,
        "highlights": [_moment_item(moment) for moment in moments[:8]],
        "prompt": (
            "今天还没有记录。可以补一条最值得记住的瞬间。"
            if not moments
            else f"今天留下了 {len(moments)} 条记录，回看其中最有感触的一条吧。"
        ),
    }
    await _append_tool_audit(ctx, tool="reviews_daily", result_count=len(moments))
    return _tool_result(ctx, "reviews_daily", result)


# ---------------------------------------------------------------------------
# 习惯 MCP Apps 工具：目标 / 打卡 / 进度
# ---------------------------------------------------------------------------


def _habit_goal_item(goal: HabitGoal) -> dict:
    return {
        "id": str(goal.id),
        "name": goal.name,
        "unit": goal.unit,
        "frequency": goal.frequency,
        "timesPerWeek": goal.times_per_week,
        "color": goal.color,
        "revision": goal.revision,
        "createdAt": goal.created_at.isoformat(),
    }


async def habit_goals_list(ctx: McpCallContext) -> CallToolResult:
    ctx.require_scope("moments.read")
    goals = await SqlHabitGoalRepository(ctx.session).list_by_user(ctx.user_id)
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "goals": [_habit_goal_item(goal) for goal in goals],
        "total": len(goals),
    }
    await _append_tool_audit(ctx, tool="habit_goals_list", result_count=len(goals))
    return _text_result(result)


async def habit_goal_create(
    ctx: McpCallContext,
    *,
    name: str,
    unit: str | None,
    frequency: str,
    times_per_week: int | None,
    color: str | None,
    idempotency_key: str,
) -> CallToolResult:
    ctx.require_scope("moments.write")
    resolved_name = name.strip()
    if not resolved_name or len(resolved_name) > 30:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="name 必须为 1~30 个字符。",
            status_code=400,
        )
    if frequency not in {"daily", "weekly"}:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="frequency 只能是 daily 或 weekly。",
            status_code=400,
        )
    if frequency == "weekly" and not times_per_week:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="weekly 目标必须提供 timesPerWeek。",
            status_code=400,
        )
    if times_per_week is not None and not 1 <= times_per_week <= 7:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="timesPerWeek 必须在 1~7 之间。",
            status_code=400,
        )
    request_body = {
        "name": resolved_name,
        "unit": unit,
        "frequency": frequency,
        "timesPerWeek": times_per_week,
        "color": color,
    }
    idem_repo = SqlIdempotencyRepository(ctx.session)
    request_fp = fingerprint_payload(request_body)
    idem_record = await idem_repo.acquire(
        user_id=ctx.user_id,
        operation="habit_goal_create",
        idempotency_key=idempotency_key,
        request_payload=request_body,
    )
    if idem_record.request_fingerprint != request_fp:
        raise ApplicationError(
            code="IDEMPOTENCY_CONFLICT",
            message="idempotencyKey 已用于不同的请求参数。",
            status_code=409,
        )
    replayed = False
    created = None
    if idem_record.state == "completed" and idem_record.resource_id:
        created = await SqlHabitGoalRepository(ctx.session).get_by_id(
            idem_record.resource_id, ctx.user_id
        )
        replayed = created is not None
    if created is None:
        now = datetime.now(UTC)
        goal = HabitGoal(
            id=uuid4(),
            user_id=ctx.user_id,
            name=resolved_name,
            unit=unit.strip() if unit else None,
            frequency=frequency,
            times_per_week=times_per_week,
            color=color,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        created = await SqlHabitGoalRepository(ctx.session).create(goal)
        await idem_repo.complete(
            record_id=idem_record.id,
            response_status=201,
            response_body={"goal": _habit_goal_item(created)},
            resource_id=created.id,
        )
    await SqlAuditEventRepository(ctx.session).append(
        user_id=ctx.user_id,
        actor_type="mcp",
        actor_id=ctx.actor_id,
        event_type="mcp.tool.habit_goal_create",
        resource_type="habit_goal",
        resource_id=created.id,
        request_id=ctx.request_id,
        allowed=True,
        metadata={"tool": "habit_goal_create", "scopes": list(ctx.scopes)},
    )
    return _text_result(
        {
            "schemaVersion": SCHEMA_VERSION,
            "created": not replayed,
            "replayed": replayed,
            "goal": _habit_goal_item(created),
        }
    )


async def habit_checkin_create(
    ctx: McpCallContext,
    *,
    goal_id: str,
    done: bool,
    count: int | None,
    occurred_at: str | None,
    timezone_name: str,
    note: str | None,
    idempotency_key: str,
) -> CallToolResult:
    ctx.require_scope("moments.write")
    try:
        goal_uuid = UUID(goal_id)
    except (ValueError, TypeError) as exc:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="goalId 不是合法的 UUID。",
            status_code=400,
            details={"goalId": goal_id},
        ) from exc
    goal = await SqlHabitGoalRepository(ctx.session).get_by_id(goal_uuid, ctx.user_id)
    if goal is None:
        raise ApplicationError(
            code="HABIT_GOAL_NOT_FOUND",
            message="未找到该习惯目标。",
            status_code=404,
            details={"goalId": goal_id},
        )
    payload: dict = {"habit": goal.name, "done": done, "goalId": goal_id}
    if goal.unit:
        payload["unit"] = goal.unit
    if count is not None:
        payload["count"] = count
    created, replayed = await _create_typed_moment(
        ctx,
        title=(f"{goal.name}打卡" if done else f"{goal.name}未完成")[:20],
        description=note,
        category="habit",
        tags=["打卡"],
        persons=None,
        event=None,
        occurred_at=occurred_at,
        occurred_local_date_time=None,
        timezone_name=timezone_name,
        moment_type="habit",
        payload=payload,
        idempotency_key=idempotency_key,
        operation="habit_checkin_create",
    )
    await _append_tool_audit(
        ctx,
        tool="habit_checkin_create",
        resource_id=created.id,
        metadata={"goalId": goal_id, "done": done, "replayed": replayed},
    )
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "created": not replayed,
        "replayed": replayed,
        "goal": _habit_goal_item(goal),
        "checkin": _moment_item(created),
    }
    return _tool_result(ctx, "habit_checkin_create", result)


async def habit_progress(
    ctx: McpCallContext,
    *,
    days: int,
    timezone_name: str | None,
) -> CallToolResult:
    ctx.require_scope("moments.read")
    zone = _parse_timezone(timezone_name)
    today = datetime.now(zone).date()
    start_day = today - timedelta(days=days - 1)
    start = datetime.combine(start_day, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    goals = await SqlHabitGoalRepository(ctx.session).list_by_user(ctx.user_id)
    checkins = await PostgresMomentRepository(ctx.session).list_by_user_range(
        ctx.user_id,
        occurred_from=start,
        occurred_to=end,
        moment_type="habit",
    )
    completed: dict[str, set[date]] = {}
    for moment in checkins:
        payload = moment.payload or {}
        if not payload.get("done"):
            continue
        goal_id = str(payload.get("goalId") or "")
        if not goal_id:
            continue
        local_date = moment.occurred_at.astimezone(zone).date()
        completed.setdefault(goal_id, set()).add(local_date)

    day_labels = [start_day + timedelta(days=offset) for offset in range(days)]
    goal_items = []
    for goal in goals:
        goal_id = str(goal.id)
        dates = completed.get(goal_id, set())
        streak = 0
        cursor = today
        while cursor in dates:
            streak += 1
            cursor -= timedelta(days=1)
        goal_items.append(
            {
                **_habit_goal_item(goal),
                "todayDone": today in dates,
                "completedDays": len(dates),
                "currentStreak": streak,
                "days": [{"date": day.isoformat(), "done": day in dates} for day in day_labels],
            }
        )
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "view": "habits",
        "timezone": timezone_name or "UTC",
        "from": start_day.isoformat(),
        "to": today.isoformat(),
        "days": days,
        "goals": goal_items,
        "total": len(goal_items),
    }
    await _append_tool_audit(ctx, tool="habit_progress", result_count=len(goal_items))
    return _tool_result(ctx, "habit_progress", result)


async def account_entitlements(ctx: McpCallContext) -> CallToolResult:
    """返回当前用户套餐与额度；该只读工具本身不消耗商业调用额度。"""
    ctx.require_scope("moments.read")
    from app.modules.entitlements.repository import EntitlementRepository
    from app.modules.quotas.repository import QuotaRepository

    entitlements = EntitlementRepository(ctx.session)
    storage = await entitlements.locked_account(ctx.user_id)
    quota_accounts = await QuotaRepository(ctx.session).ensure_current_accounts(ctx.user_id)
    return _text_result(
        {
            "schemaVersion": SCHEMA_VERSION,
            "planKey": await entitlements.current_plan_key(ctx.user_id),
            "storage": {
                "usedBytes": storage.used_bytes,
                "reservedBytes": storage.reserved_bytes,
                "effectiveQuotaBytes": storage.effective_quota_bytes,
                "overQuota": storage.over_quota,
            },
            "quotas": [
                {
                    "key": item.quota_key,
                    "limit": item.limit_value,
                    "used": item.used_value,
                    "reserved": item.reserved_value,
                    "remaining": max(0, item.limit_value - item.used_value - item.reserved_value),
                    "periodStart": item.period_start.isoformat(),
                    "periodEnd": item.period_end.isoformat() if item.period_end else None,
                }
                for item in quota_accounts
            ],
        }
    )


# ---------------------------------------------------------------------------
# 通用工具规划与 A2UI Action
# ---------------------------------------------------------------------------


async def reminder_create(
    ctx: McpCallContext,
    *,
    title: str,
    note: str | None,
    scene: str,
    remind_at: str | None,
    local_date_time: str | None,
    after_minutes: int | None,
    deadline_at: str | None,
    timezone_name: str | None,
    idempotency_key: str,
) -> CallToolResult:
    """创建由 Server 调度的提醒；MCP 与 Web 共用同一通知管线。"""
    ctx.require_scope("moments.write")
    timezone_name = await _resolve_timezone(ctx, timezone_name)
    parsed_remind_at, time_source = resolve_reminder_time(
        remind_at=remind_at,
        local_date_time=local_date_time,
        after_minutes=after_minutes,
        timezone_name=timezone_name,
    )
    try:
        parsed_deadline_at = (
            datetime.fromisoformat(deadline_at.replace("Z", "+00:00")) if deadline_at else None
        )
        if parsed_deadline_at is not None and parsed_deadline_at.tzinfo is None:
            raise ValueError("dueAt must include an offset")
    except ValueError as exc:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="remindAt 和 dueAt 必须是带时区的 ISO-8601 时间。",
            status_code=400,
        ) from exc
    request_body = {
        "title": title,
        "note": note,
        "scene": scene,
        "remindAt": remind_at,
        "localDateTime": local_date_time,
        "afterMinutes": after_minutes,
        "dueAt": deadline_at,
        "timezone": timezone_name,
    }
    idempotency = SqlIdempotencyRepository(ctx.session)
    record = await idempotency.acquire(
        user_id=ctx.user_id,
        operation="reminder_create",
        idempotency_key=idempotency_key,
        request_payload=request_body,
    )
    if record.request_fingerprint != fingerprint_payload(request_body):
        raise ApplicationError(
            code="IDEMPOTENCY_CONFLICT",
            message="idempotencyKey 已用于不同的请求体。",
            status_code=409,
        )
    if record.state == "completed" and record.response_body is not None:
        return _text_result(record.response_body)
    reminder = await ReminderService(
        ReminderRepository(ctx.session),
        NotificationPipelineRepository(ctx.session),
    ).create(
        user_id=ctx.user_id,
        title=title,
        body=note,
        scene=scene,
        due_at=parsed_remind_at,
        deadline_at=parsed_deadline_at,
        timezone=timezone_name,
        source_type="mcp",
        correlation_id=ctx.request_id or idempotency_key,
    )
    response = {
        "reminder": serialize_reminder(reminder),
        "resolvedTime": {
            "remindAt": reminder.due_at.isoformat(),
            "timezone": reminder.timezone,
            "source": time_source,
        },
    }
    await idempotency.complete(
        record_id=record.id,
        response_status=201,
        response_body=response,
        resource_id=reminder.id,
    )
    await _append_tool_audit(
        ctx,
        tool="reminder_create",
        resource_id=reminder.id,
        metadata={"scene": scene},
    )
    return _text_result(response)


_TOOL_SCOPE: dict[str, str] = {
    "bookkeeping_create": "moments.write",
    "bookkeeping_list": "moments.read",
    "bookkeeping_summary": "moments.read",
    "moments_create": "moments.write",
    "moments_list": "moments.read",
    "moments_search": "moments.read",
    "moments_count": "moments.read",
    "moments_get": "moments.read",
    "reviews_daily": "moments.read",
    "habit_goals_list": "moments.read",
    "habit_goal_create": "moments.write",
    "habit_checkin_create": "moments.write",
    "habit_progress": "moments.read",
    "reminder_create": "moments.write",
    "a2ui_action": "moments.read",
}


def _plan_none(reply: str) -> CallToolResult:
    return _text_result({"toolName": "", "arguments": {}, "reply": reply})


def _validate_planned_tool(
    ctx: McpCallContext,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    tool_schemas: dict[str, dict],
) -> CallToolResult:
    if tool_name == "agent_plan" or tool_name not in tool_schemas:
        return _plan_none("当前服务端没有可安全执行的对应工具。")
    required_scope = _TOOL_SCOPE.get(tool_name)
    if required_scope and not has_scope(ctx.scopes, required_scope):
        return _plan_none(f"当前授权缺少 {required_scope}，无法执行这个操作。")
    if ctx.available_tools is not None and tool_name not in ctx.available_tools:
        return _plan_none("当前订阅、授权或剩余额度暂时不能执行这个工具。")
    validator = Draft202012Validator(
        tool_schemas[tool_name],
        format_checker=FormatChecker(),
    )
    errors = sorted(validator.iter_errors(arguments), key=lambda error: list(error.path))
    if errors:
        return _plan_none(f"还需要补充信息：{errors[0].message}")
    return _text_result({"toolName": tool_name, "arguments": arguments, "reply": ""})


def _extract_moment_query(text: str) -> str:
    query = re.sub(r"^(帮我|请|查找|搜索|找一下|找找|看看|查看)", "", text).strip()
    query = re.sub(r"(的)?(记忆|记录|Moment|moment)$", "", query).strip()
    return query


async def agent_plan(
    ctx: McpCallContext,
    *,
    input: str,
    tool_schemas: dict[str, dict],
) -> CallToolResult:
    """把用户原话规划为一个当前已注册、参数合法且 Scope 允许的 MCP Tool。"""
    text = str(input or "").strip()
    if not text:
        return _plan_none("请告诉我你想记录或查询什么。")
    now = datetime.now(UTC)

    if re.search(r"花了多少|用了多少|收支|结余|开销|账单统计|支出.*多少|收入.*多少", text):
        period = _resolve_period(text, now)
        arguments: dict[str, Any] = {"period": period["period"]}
        if period.get("from") is not None:
            arguments["from_"] = period["from"]
            arguments["to"] = period["to"]
        if period.get("year") is not None:
            arguments["year"] = period["year"]
        if period.get("month") is not None:
            arguments["month"] = period["month"]
        return _validate_planned_tool(
            ctx,
            tool_name="bookkeeping_summary",
            arguments=arguments,
            tool_schemas=tool_schemas,
        )

    if re.search(r"记(?:一笔|账)|花了|消费|支出|收入|赚了|入账", text):
        create_arguments = _resolve_create(text, now)
        if create_arguments is None:
            return _plan_none("请补充金额，例如“记一笔午餐 28 元”。")
        arguments = dict(create_arguments)
        return _validate_planned_tool(
            ctx,
            tool_name="bookkeeping_create",
            arguments=arguments,
            tool_schemas=tool_schemas,
        )

    if re.search(r"账单|流水|消费记录|收支明细|账目明细", text):
        return _validate_planned_tool(
            ctx,
            tool_name="bookkeeping_list",
            arguments={"limit": 20},
            tool_schemas=tool_schemas,
        )

    if re.search(r"打卡|完成了", text):
        goals = await SqlHabitGoalRepository(ctx.session).list_by_user(ctx.user_id)
        matches = [goal for goal in goals if goal.name in text]
        if len(matches) != 1:
            names = "、".join(goal.name for goal in goals[:5])
            return _plan_none(f"请说明要打卡的习惯。当前习惯：{names or '暂无习惯目标'}。")
        goal = matches[0]
        count_match = re.search(r"(\d+)\s*(?:次|分钟|公里|杯)?", text)
        arguments = {
            "goalId": str(goal.id),
            "done": True,
            "timezone": "UTC",
            "idempotencyKey": str(uuid4()),
        }
        if count_match:
            arguments["count"] = int(count_match.group(1))
        return _validate_planned_tool(
            ctx,
            tool_name="habit_checkin_create",
            arguments=arguments,
            tool_schemas=tool_schemas,
        )

    if re.search(r"习惯.*(进度|完成|情况)|查看.*习惯|我的习惯", text):
        return _validate_planned_tool(
            ctx,
            tool_name="habit_progress",
            arguments={"days": 7, "timezone": "UTC"},
            tool_schemas=tool_schemas,
        )

    if re.search(r"习惯目标|有哪些习惯", text):
        return _validate_planned_tool(
            ctx,
            tool_name="habit_goals_list",
            arguments={},
            tool_schemas=tool_schemas,
        )

    moment_id = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
        text,
    )
    if moment_id and re.search(r"查看|详情|这条", text):
        return _validate_planned_tool(
            ctx,
            tool_name="moments_get",
            arguments={"momentId": moment_id.group(0)},
            tool_schemas=tool_schemas,
        )

    if re.search(r"最近.*(记录|Moment|记忆)|列出.*(记录|Moment|记忆)|时间线", text):
        return _validate_planned_tool(
            ctx,
            tool_name="moments_list",
            arguments={"limit": 10},
            tool_schemas=tool_schemas,
        )

    if re.search(r"搜索|查找|找一下|哪次|什么时候|关于.*(记忆|记录)", text):
        query = _extract_moment_query(text)
        if not query:
            return _plan_none("请补充要搜索的关键词。")
        return _validate_planned_tool(
            ctx,
            tool_name="moments_search",
            arguments={"query": query, "limit": 10},
            tool_schemas=tool_schemas,
        )

    return _plan_none(
        "暂时无法确定要调用哪个工具。可以尝试记账、查询 Moment、查看习惯进度或习惯打卡。"
    )


async def a2ui_action(
    ctx: McpCallContext,
    *,
    name: str,
    context: dict,
    surface_id: str,
) -> CallToolResult:
    """把白名单 A2UI 只读 Action 转换为后续真实 Tool 计划，不直接执行。"""
    ctx.require_scope("moments.read")
    if not surface_id or len(surface_id) > 120:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="surfaceId 无效。",
            status_code=400,
        )
    if name == "open_detail":
        moment_id = context.get("momentId")
        try:
            UUID(str(moment_id))
        except (ValueError, TypeError) as exc:
            raise ApplicationError(
                code="INVALID_ARGUMENTS",
                message="open_detail 需要合法的 momentId。",
                status_code=400,
            ) from exc
        result = {
            "accepted": True,
            "name": name,
            "surfaceId": surface_id,
            "toolName": "moments_get",
            "arguments": {"momentId": str(moment_id)},
        }
    elif name == "refresh":
        view = context.get("view")
        refresh_map: dict[str, tuple[str, dict[str, Any]]] = {
            "timeline": ("moments_list", {"limit": 10}),
            "habits": ("habit_progress", {"days": 7, "timezone": "UTC"}),
            "bookkeeping": ("bookkeeping_summary", {"period": "month"}),
        }
        if view not in refresh_map:
            raise ApplicationError(
                code="INVALID_ARGUMENTS",
                message="refresh view 不在白名单中。",
                status_code=400,
            )
        tool_name, arguments = refresh_map[str(view)]
        result = {
            "accepted": True,
            "name": name,
            "surfaceId": surface_id,
            "toolName": tool_name,
            "arguments": arguments,
        }
    else:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="A2UI Action 不在白名单中。",
            status_code=400,
        )
    await _append_tool_audit(
        ctx,
        tool="a2ui_action",
        metadata={"action": name, "surfaceId": surface_id},
    )
    return _text_result(result)


__all__ = [
    "McpCallContext",
    "bookkeeping_create",
    "bookkeeping_list",
    "bookkeeping_summary",
    "bookkeeping_plan",
    "moments_create",
    "moments_list",
    "moments_search",
    "moments_count",
    "reviews_daily",
    "moments_get",
    "habit_goals_list",
    "habit_goal_create",
    "habit_checkin_create",
    "habit_progress",
    "reminder_create",
    "agent_plan",
    "a2ui_action",
    "err_result",
]
