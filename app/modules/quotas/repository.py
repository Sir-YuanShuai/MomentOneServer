from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApplicationError
from app.infrastructure.database.models import (
    DeviceBinding,
    McpAuthorization,
    QuotaAccount,
    QuotaUsageEvent,
    UserEntitlement,
)
from app.modules.entitlements.repository import EntitlementRepository

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class QuotaPeriod:
    start: datetime
    end: datetime | None


def quota_period(quota_key: str, now: datetime | None = None) -> QuotaPeriod:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if quota_key.endswith(".day"):
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return QuotaPeriod(start=start, end=start + timedelta(days=1))
    if quota_key.endswith(".month"):
        start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return QuotaPeriod(start=start, end=end)
    return QuotaPeriod(start=EPOCH, end=None)


class QuotaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._entitlements = EntitlementRepository(session)

    async def plan_limits(self, user_id: UUID) -> dict[str, int]:
        plan = await self._entitlements.get_plan(await self._entitlements.current_plan_key(user_id))
        if plan is None:
            return {}
        return {
            key: value
            for key, value in plan.quotas.items()
            if isinstance(value, int) and value >= 0 and "." in key
        }

    async def active_entitlements(self, user_id: UUID) -> frozenset[str]:
        now = datetime.now(UTC)
        rows = (
            await self._session.execute(
                select(UserEntitlement.entitlement_key).where(
                    UserEntitlement.user_id == user_id,
                    UserEntitlement.status == "active",
                    UserEntitlement.starts_at <= now,
                    (UserEntitlement.expires_at.is_(None) | (UserEntitlement.expires_at > now)),
                )
            )
        ).scalars()
        return frozenset(rows.all())

    async def _resource_used(self, user_id: UUID, quota_key: str) -> int | None:
        if quota_key == "device.active":
            return int(
                await self._session.scalar(
                    select(func.count(DeviceBinding.id)).where(
                        DeviceBinding.user_id == user_id, DeviceBinding.status == "active"
                    )
                )
                or 0
            )
        if quota_key == "mcp.clients.active":
            return int(
                await self._session.scalar(
                    select(func.count(McpAuthorization.id)).where(
                        McpAuthorization.user_id == user_id,
                        McpAuthorization.client_type == "mcp",
                        McpAuthorization.status == "active",
                    )
                )
                or 0
            )
        return None

    async def get_or_create_account(
        self,
        user_id: UUID,
        quota_key: str,
        *,
        lock: bool = False,
    ) -> QuotaAccount:
        limits = await self.plan_limits(user_id)
        limit_value = int(limits.get(quota_key, 0))
        period = quota_period(quota_key)
        stmt = select(QuotaAccount).where(
            QuotaAccount.user_id == user_id,
            QuotaAccount.quota_key == quota_key,
            QuotaAccount.period_start == period.start,
        )
        if lock:
            stmt = stmt.with_for_update()
        account = await self._session.scalar(stmt)
        if account is None:
            used_value = await self._resource_used(user_id, quota_key) or 0
            account = QuotaAccount(
                user_id=user_id,
                quota_key=quota_key,
                limit_value=limit_value,
                used_value=used_value,
                reserved_value=0,
                period_start=period.start,
                period_end=period.end,
                revision=1,
            )
            self._session.add(account)
            await self._session.flush()
            return account
        resource_used = await self._resource_used(user_id, quota_key)
        changed = False
        if account.limit_value != limit_value:
            account.limit_value = limit_value
            changed = True
        if resource_used is not None and account.used_value != resource_used:
            account.used_value = resource_used
            changed = True
        if changed:
            account.revision += 1
            await self._session.flush()
        return account

    async def ensure_current_accounts(self, user_id: UUID) -> list[QuotaAccount]:
        accounts = []
        for quota_key in sorted(await self.plan_limits(user_id)):
            accounts.append(await self.get_or_create_account(user_id, quota_key))
        return accounts

    @staticmethod
    def _raise_exceeded(account: QuotaAccount, amount: int = 1) -> None:
        raise ApplicationError(
            code="QUOTA_EXCEEDED",
            message="当前订阅额度已用完。",
            status_code=429,
            details={
                "quotaKey": account.quota_key,
                "limit": account.limit_value,
                "used": account.used_value,
                "reserved": account.reserved_value,
                "requested": amount,
                "resetAt": account.period_end.isoformat() if account.period_end else None,
            },
        )

    async def check_available(self, user_id: UUID, quota_key: str, *, amount: int = 1) -> bool:
        account = await self.get_or_create_account(user_id, quota_key)
        return account.used_value + account.reserved_value + amount <= account.limit_value

    async def require_available(self, user_id: UUID, quota_key: str, *, amount: int = 1) -> None:
        account = await self.get_or_create_account(user_id, quota_key)
        if account.used_value + account.reserved_value + amount > account.limit_value:
            self._raise_exceeded(account, amount)

    async def require_resource_capacity(
        self, user_id: UUID, quota_key: str, *, next_value: int
    ) -> None:
        account = await self.get_or_create_account(user_id, quota_key, lock=True)
        if next_value > account.limit_value:
            code = (
                "DEVICE_LIMIT_EXCEEDED"
                if quota_key == "device.active"
                else "MCP_CLIENT_LIMIT_EXCEEDED"
            )
            raise ApplicationError(
                code=code,
                message="当前订阅允许的活跃连接数量已达到上限。",
                status_code=409,
                details={
                    "quotaKey": quota_key,
                    "limit": account.limit_value,
                    "used": account.used_value,
                    "requestedValue": next_value,
                },
            )

    async def consume(
        self,
        user_id: UUID,
        quota_key: str,
        *,
        amount: int,
        operation_key: str,
        actor_type: str,
        tool_name: str | None = None,
        client_id: str | None = None,
        device_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> QuotaAccount:
        existing = await self._session.scalar(
            select(QuotaUsageEvent.id).where(
                QuotaUsageEvent.user_id == user_id,
                QuotaUsageEvent.quota_key == quota_key,
                QuotaUsageEvent.operation_key == operation_key,
            )
        )
        account = await self.get_or_create_account(user_id, quota_key, lock=True)
        if existing is not None:
            return account
        if account.used_value + account.reserved_value + amount > account.limit_value:
            self._raise_exceeded(account, amount)
        self._session.add(
            QuotaUsageEvent(
                user_id=user_id,
                quota_key=quota_key,
                amount=amount,
                operation_key=operation_key,
                actor_type=actor_type,
                tool_name=tool_name,
                client_id=client_id,
                device_id=device_id,
                idempotency_key=idempotency_key,
                metadata_=metadata or {},
            )
        )
        account.used_value += amount
        account.revision += 1
        await self._session.flush()
        return account
