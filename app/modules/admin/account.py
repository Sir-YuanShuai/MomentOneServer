from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import (
    DeviceBinding,
    McpAuthorization,
    PlanDefinition,
    QuotaAccount,
    QuotaUsageEvent,
    User,
)
from app.modules.entitlements.repository import EntitlementRepository
from app.modules.quotas.repository import QuotaRepository


class AccountRepository:
    """用户账户可见信息与管理员用户详情聚合。

    该 Repository 只返回面向 API 的安全投影，不暴露数据库表名或内部存储实现。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._entitlements = EntitlementRepository(session)
        self._quotas = QuotaRepository(session)

    async def account(
        self, user_id: UUID, *, avatar_url: str | None = None
    ) -> dict[str, object] | None:
        user = await self._session.scalar(select(User).where(User.id == user_id))
        if user is None:
            return None

        account = await self._entitlements.locked_account(user_id)
        plan_key = await self._entitlements.current_plan_key(user_id)
        plan = await self._session.scalar(
            select(PlanDefinition).where(PlanDefinition.key == plan_key)
        )
        entitlements = await self._entitlements.list_user_entitlements(user_id)
        now = datetime.now(UTC)
        effective_entitlements = [
            item
            for item in entitlements
            if item.status == "active"
            and item.starts_at <= now
            and (item.expires_at is None or item.expires_at > now)
        ]
        local_avatar = user.avatar_url
        return {
            "user": {
                "id": str(user.id),
                "status": user.status,
                "createdAt": user.created_at.isoformat(),
                "lastActiveAt": user.last_active_at.isoformat() if user.last_active_at else None,
            },
            "profile": {
                "displayName": user.display_name,
                "email": user.email,
                "avatarUrl": avatar_url or local_avatar,
            },
            "plan": self._plan_dict(plan, fallback_key=plan_key),
            "storage": {
                "usedBytes": account.used_bytes,
                "reservedBytes": account.reserved_bytes,
                "effectiveQuotaBytes": account.effective_quota_bytes,
                "availableBytes": max(
                    0,
                    account.effective_quota_bytes - account.used_bytes - account.reserved_bytes,
                ),
                "overQuota": account.over_quota,
                "revision": account.revision,
                "updatedAt": account.updated_at.isoformat(),
            },
            "entitlements": [
                {
                    "key": item.entitlement_key,
                    "sourceType": item.source_type,
                    "status": "active",
                    "startsAt": item.starts_at.isoformat(),
                    "expiresAt": item.expires_at.isoformat() if item.expires_at else None,
                }
                for item in effective_entitlements
            ],
            "quotaAccounts": await self._quota_accounts(user_id, now=now),
        }

    async def admin_user_detail(self, user_id: UUID) -> dict[str, object] | None:
        account = await self.account(user_id)
        if account is None:
            return None
        user = await self._session.scalar(select(User).where(User.id == user_id))
        if user is None:  # pragma: no cover - protected by account()
            return None

        since = datetime.now(UTC) - timedelta(days=30)
        usage_summary = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(QuotaUsageEvent.amount), 0),
                    func.count(QuotaUsageEvent.id),
                    func.count(func.distinct(cast(QuotaUsageEvent.occurred_at, Date))),
                ).where(
                    QuotaUsageEvent.user_id == user_id,
                    QuotaUsageEvent.occurred_at >= since,
                )
            )
        ).one()
        by_quota_rows = (
            await self._session.execute(
                select(
                    QuotaUsageEvent.quota_key,
                    func.sum(QuotaUsageEvent.amount),
                    func.count(QuotaUsageEvent.id),
                )
                .where(
                    QuotaUsageEvent.user_id == user_id,
                    QuotaUsageEvent.occurred_at >= since,
                )
                .group_by(QuotaUsageEvent.quota_key)
                .order_by(func.sum(QuotaUsageEvent.amount).desc())
            )
        ).all()
        device_total, device_active = (
            await self._session.execute(
                select(
                    func.count(DeviceBinding.id),
                    func.count(DeviceBinding.id).filter(DeviceBinding.status == "active"),
                ).where(DeviceBinding.user_id == user_id)
            )
        ).one()
        mcp_total, mcp_active = (
            await self._session.execute(
                select(
                    func.count(McpAuthorization.id),
                    func.count(McpAuthorization.id).filter(McpAuthorization.status == "active"),
                ).where(McpAuthorization.user_id == user_id)
            )
        ).one()
        user_summary = account.get("user")
        if not isinstance(user_summary, dict):  # pragma: no cover - internal projection invariant
            user_summary = {}
        return {
            **account,
            "user": {
                **user_summary,
                "casdoorSub": user.casdoor_sub,
                "revision": user.revision,
                "updatedAt": user.updated_at.isoformat(),
                "disabledAt": user.disabled_at.isoformat() if user.disabled_at else None,
                "disableReason": user.disable_reason,
            },
            "quotaUsage30d": {
                "from": since.isoformat(),
                "totalAmount": int(usage_summary[0] or 0),
                "eventCount": int(usage_summary[1] or 0),
                "activeDays": int(usage_summary[2] or 0),
                "byQuota": [
                    {
                        "quotaKey": quota_key,
                        "amount": int(amount or 0),
                        "eventCount": int(event_count or 0),
                    }
                    for quota_key, amount, event_count in by_quota_rows
                ],
            },
            "access": {
                "deviceBindings": {
                    "total": int(device_total or 0),
                    "active": int(device_active or 0),
                },
                "mcpAuthorizations": {"total": int(mcp_total or 0), "active": int(mcp_active or 0)},
            },
        }

    async def _quota_accounts(self, user_id: UUID, *, now: datetime) -> list[dict[str, object]]:
        del now  # QuotaRepository 统一按 UTC 选择当前日/月周期。
        rows = await self._quotas.ensure_current_accounts(user_id)
        return [self._quota_account_dict(item) for item in rows]

    @staticmethod
    def _plan_dict(plan: PlanDefinition | None, *, fallback_key: str) -> dict[str, object]:
        if plan is None:
            return {
                "key": fallback_key,
                "version": None,
                "name": fallback_key,
                "status": "unknown",
                "entitlements": {},
                "quotas": {},
            }
        return {
            "key": plan.key,
            "version": plan.version,
            "name": plan.name,
            "status": plan.status,
            "entitlements": dict(plan.entitlements or {}),
            "quotas": dict(plan.quotas or {}),
        }

    @staticmethod
    def _quota_account_dict(item: QuotaAccount) -> dict[str, object]:
        remaining = max(0, item.limit_value - item.used_value - item.reserved_value)
        return {
            "quotaKey": item.quota_key,
            "limit": item.limit_value,
            "used": item.used_value,
            "reserved": item.reserved_value,
            "remaining": remaining,
            "periodStart": item.period_start.isoformat(),
            "periodEnd": item.period_end.isoformat() if item.period_end else None,
            "revision": item.revision,
            "updatedAt": item.updated_at.isoformat(),
        }


__all__ = ["AccountRepository"]
