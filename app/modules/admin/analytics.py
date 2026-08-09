from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import ApiUsageBucket, QuotaUsageEvent, User

MCP_TOOL_CALLS = "mcp.tool_calls.month"
MCP_WRITE_CALLS = "mcp.write_calls.month"
AGENT_PLAN_CALLS = "mcp.agent_plan.day"
AI_TOKENS = "ai.tokens.month"


class AdminAnalyticsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def usage_overview(self, *, days: int) -> dict[str, object]:
        now = datetime.now(UTC)
        first_day = now.date() - timedelta(days=days - 1)
        start = datetime.combine(first_day, time.min, tzinfo=UTC)
        today_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
        monthly_start = now - timedelta(days=30)

        today_active = await self._session.scalar(
            select(func.count(User.id)).where(User.last_active_at >= today_start)
        )
        monthly_active = await self._session.scalar(
            select(func.count(User.id)).where(User.last_active_at >= monthly_start)
        )

        api_requests, api_errors = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(ApiUsageBucket.request_count), 0),
                    func.coalesce(func.sum(ApiUsageBucket.error_count), 0),
                ).where(ApiUsageBucket.bucket_start >= start)
            )
        ).one()
        quota_totals = {
            str(key): int(amount or 0)
            for key, amount in (
                await self._session.execute(
                    select(QuotaUsageEvent.quota_key, func.sum(QuotaUsageEvent.amount))
                    .where(QuotaUsageEvent.occurred_at >= start)
                    .group_by(QuotaUsageEvent.quota_key)
                )
            ).all()
        }

        api_daily_rows = (
            await self._session.execute(
                select(
                    cast(ApiUsageBucket.bucket_start, Date).label("day"),
                    func.sum(ApiUsageBucket.request_count),
                    func.sum(ApiUsageBucket.error_count),
                )
                .where(ApiUsageBucket.bucket_start >= start)
                .group_by("day")
                .order_by("day")
            )
        ).all()
        quota_daily_rows = (
            await self._session.execute(
                select(
                    cast(QuotaUsageEvent.occurred_at, Date).label("day"),
                    QuotaUsageEvent.quota_key,
                    func.sum(QuotaUsageEvent.amount),
                )
                .where(QuotaUsageEvent.occurred_at >= start)
                .group_by("day", QuotaUsageEvent.quota_key)
                .order_by("day")
            )
        ).all()

        series = {
            first_day + timedelta(days=offset): {
                "date": (first_day + timedelta(days=offset)).isoformat(),
                "apiRequests": 0,
                "apiErrors": 0,
                "mcpToolCalls": 0,
                "mcpWriteCalls": 0,
                "agentPlanCalls": 0,
                "aiTokens": 0,
            }
            for offset in range(days)
        }
        for day, requests, errors in api_daily_rows:
            parsed = self._date_value(day)
            if parsed in series:
                series[parsed]["apiRequests"] = int(requests or 0)
                series[parsed]["apiErrors"] = int(errors or 0)
        quota_field = {
            MCP_TOOL_CALLS: "mcpToolCalls",
            MCP_WRITE_CALLS: "mcpWriteCalls",
            AGENT_PLAN_CALLS: "agentPlanCalls",
            AI_TOKENS: "aiTokens",
        }
        for day, quota_key, amount in quota_daily_rows:
            parsed = self._date_value(day)
            field = quota_field.get(str(quota_key))
            if parsed in series and field:
                series[parsed][field] = int(amount or 0)

        endpoint_rows = (
            await self._session.execute(
                select(
                    ApiUsageBucket.route,
                    ApiUsageBucket.method,
                    func.sum(ApiUsageBucket.request_count).label("requests"),
                    func.sum(ApiUsageBucket.error_count).label("errors"),
                    func.sum(ApiUsageBucket.latency_ms_total).label("latency"),
                )
                .where(ApiUsageBucket.bucket_start >= start)
                .group_by(ApiUsageBucket.route, ApiUsageBucket.method)
                .order_by(func.sum(ApiUsageBucket.request_count).desc())
                .limit(20)
            )
        ).all()
        tool_rows = (
            await self._session.execute(
                select(QuotaUsageEvent.tool_name, func.sum(QuotaUsageEvent.amount).label("calls"))
                .where(
                    QuotaUsageEvent.occurred_at >= start,
                    QuotaUsageEvent.quota_key == MCP_TOOL_CALLS,
                    QuotaUsageEvent.tool_name.is_not(None),
                )
                .group_by(QuotaUsageEvent.tool_name)
                .order_by(func.sum(QuotaUsageEvent.amount).desc())
                .limit(20)
            )
        ).all()

        return {
            "generatedAt": now.isoformat(),
            "timezone": "UTC",
            "days": days,
            "from": start.isoformat(),
            "to": now.isoformat(),
            "todayActive": int(today_active or 0),
            "monthlyActive": int(monthly_active or 0),
            "apiRequests": int(api_requests or 0),
            "apiErrors": int(api_errors or 0),
            "mcpToolCalls": quota_totals.get(MCP_TOOL_CALLS, 0),
            "mcpWriteCalls": quota_totals.get(MCP_WRITE_CALLS, 0),
            "agentPlanCalls": quota_totals.get(AGENT_PLAN_CALLS, 0),
            "aiTokens": quota_totals.get(AI_TOKENS, 0),
            "series": list(series.values()),
            "endpoints": [
                {
                    "route": route,
                    "method": method,
                    "requestCount": int(requests or 0),
                    "errorCount": int(errors or 0),
                    "averageLatencyMs": round(int(latency or 0) / int(requests or 1), 2),
                }
                for route, method, requests, errors, latency in endpoint_rows
            ],
            "topTools": [
                {"toolName": tool_name, "callCount": int(calls or 0)}
                for tool_name, calls in tool_rows
                if tool_name
            ],
        }

    @staticmethod
    def _date_value(value: date | datetime | str) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(value)


__all__ = ["AdminAnalyticsRepository"]
