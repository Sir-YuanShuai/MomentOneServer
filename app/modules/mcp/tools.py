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

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from mcp.types import CallToolResult, TextContent
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.audit_event_repository import (
    SqlAuditEventRepository,
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
from app.modules.mcp.scope import has_scope
from app.modules.moment_types.registry import validate as validate_moment_type
from app.modules.moments.domain import (
    Moment,
    MomentCategory,
    MomentProvenance,
    ProvenanceSource,
)

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


def _parse_occurred_at(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="occurredAt 不是合法的 ISO-8601 时间。",
            status_code=400,
            details={"occurredAt": value},
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


# ---------------------------------------------------------------------------
# 工具：bookkeeping_create
# ---------------------------------------------------------------------------


async def bookkeeping_create(
    ctx: McpCallContext,
    *,
    amount: float,
    flow: str,
    occurred_at: str | None,
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

    occurred = _parse_occurred_at(occurred_at)

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
            return _text_result(idem_record.response_body)

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
        timezone="UTC",
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
        "revision": created.revision,
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

    return _text_result(response)


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

    return _text_result(result)


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
) -> CallToolResult:
    ctx.require_scope("moments.read")

    if period not in ("month", "quarter", "year"):
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="period 必须是 month / quarter / year。",
            status_code=400,
            details={"period": period},
        )

    start, end, label = _period_bounds(period, year, month)

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
        "from": start.isoformat(),
        "to": end.isoformat(),
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
            "period": period,
            "resultCount": count,
        },
    )

    return _text_result(result)


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

    return _text_result(result)


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


__all__ = [
    "McpCallContext",
    "bookkeeping_create",
    "bookkeeping_list",
    "bookkeeping_summary",
    "moments_get",
    "err_result",
]
