from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import (
    StorageQuotaGrant,
    User,
    UserEntitlement,
    UserStorageAccount,
)
from app.modules.entitlements.repository import EntitlementRepository


class AdminEntitlementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._entitlements = EntitlementRepository(session)

    async def summary(self) -> dict[str, object]:
        used, reserved, quota, accounts, over = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(UserStorageAccount.used_bytes), 0),
                    func.coalesce(func.sum(UserStorageAccount.reserved_bytes), 0),
                    func.coalesce(func.sum(UserStorageAccount.effective_quota_bytes), 0),
                    func.count(UserStorageAccount.user_id),
                    func.count(UserStorageAccount.user_id).filter(UserStorageAccount.over_quota),
                )
            )
        ).one()
        return {
            "generatedAt": datetime.now(UTC).isoformat(),
            "accountCount": int(accounts or 0),
            "usedBytes": int(used or 0),
            "reservedBytes": int(reserved or 0),
            "effectiveQuotaBytes": int(quota or 0),
            "overQuotaCount": int(over or 0),
        }

    async def list_accounts(
        self, *, query: str | None, over_quota: bool | None, limit: int
    ) -> list[dict[str, object]]:
        grant_count = (
            select(func.count(StorageQuotaGrant.id))
            .where(
                StorageQuotaGrant.user_id == User.id,
                StorageQuotaGrant.status == "active",
            )
            .correlate(User)
            .scalar_subquery()
        )
        stmt = (
            select(User, UserStorageAccount, grant_count)
            .join(UserStorageAccount, UserStorageAccount.user_id == User.id)
            .order_by(UserStorageAccount.over_quota.desc(), UserStorageAccount.used_bytes.desc())
            .limit(limit)
        )
        if query:
            pattern = f"%{query.strip()}%"
            stmt = stmt.where(
                or_(
                    User.display_name.ilike(pattern),
                    User.email.ilike(pattern),
                    User.casdoor_sub.ilike(pattern),
                )
            )
        if over_quota is not None:
            stmt = stmt.where(UserStorageAccount.over_quota == over_quota)
        rows = (await self._session.execute(stmt)).all()
        items: list[dict[str, object]] = []
        for user, account, active_grants in rows:
            items.append(
                {
                    "userId": str(user.id),
                    "displayName": user.display_name,
                    "email": user.email,
                    "status": user.status,
                    "planKey": await self._entitlements.current_plan_key(user.id),
                    "usedBytes": account.used_bytes,
                    "reservedBytes": account.reserved_bytes,
                    "effectiveQuotaBytes": account.effective_quota_bytes,
                    "overQuota": account.over_quota,
                    "revision": account.revision,
                    "activeGrantCount": int(active_grants or 0),
                    "reconciledAt": (
                        account.reconciled_at.isoformat() if account.reconciled_at else None
                    ),
                    "updatedAt": account.updated_at.isoformat(),
                }
            )
        return items

    async def account_detail(self, user_id: UUID) -> dict[str, object] | None:
        user = await self._session.scalar(select(User).where(User.id == user_id))
        if user is None:
            return None
        account = await self._entitlements.locked_account(user_id)
        entitlements = await self._entitlements.list_user_entitlements(user_id)
        grants = await self._entitlements.list_storage_grants(user_id)
        return {
            "user": {
                "id": str(user.id),
                "displayName": user.display_name,
                "email": user.email,
                "status": user.status,
            },
            "account": {
                "userId": str(user.id),
                "planKey": await self._entitlements.current_plan_key(user_id),
                "usedBytes": account.used_bytes,
                "reservedBytes": account.reserved_bytes,
                "effectiveQuotaBytes": account.effective_quota_bytes,
                "overQuota": account.over_quota,
                "revision": account.revision,
                "reconciledAt": account.reconciled_at.isoformat()
                if account.reconciled_at
                else None,
                "updatedAt": account.updated_at.isoformat(),
            },
            "entitlements": [self._entitlement_dict(item) for item in entitlements],
            "storageGrants": [self._grant_dict(item) for item in grants],
        }

    @staticmethod
    def _entitlement_dict(item: UserEntitlement) -> dict[str, object]:
        status = item.status
        if status == "active" and item.expires_at and item.expires_at <= datetime.now(UTC):
            status = "expired"
        return {
            "id": str(item.id),
            "key": item.entitlement_key,
            "sourceType": item.source_type,
            "sourceRef": item.source_ref,
            "status": status,
            "startsAt": item.starts_at.isoformat(),
            "expiresAt": item.expires_at.isoformat() if item.expires_at else None,
            "metadata": item.metadata_,
            "revision": item.revision,
        }

    @staticmethod
    def _grant_dict(item: StorageQuotaGrant) -> dict[str, object]:
        status = item.status
        if status == "active" and item.expires_at and item.expires_at <= datetime.now(UTC):
            status = "expired"
        return {
            "id": str(item.id),
            "sourceType": item.source_type,
            "sourceRef": item.source_ref,
            "quotaBytes": item.quota_bytes,
            "status": status,
            "startsAt": item.starts_at.isoformat(),
            "expiresAt": item.expires_at.isoformat() if item.expires_at else None,
            "revision": item.revision,
            "createdAt": item.created_at.isoformat(),
        }

    async def account_revision(self, user_id: UUID) -> int | None:
        return await self._session.scalar(
            select(UserStorageAccount.revision).where(UserStorageAccount.user_id == user_id)
        )

    async def set_plan(
        self, user_id: UUID, *, plan_key: str, expected_revision: int
    ) -> dict[str, object] | None:
        account = await self._entitlements.set_plan(
            user_id, plan_key=plan_key, expected_revision=expected_revision
        )
        return await self.account_detail(user_id) if account is not None else None

    async def add_storage_grant(
        self,
        user_id: UUID,
        *,
        quota_bytes: int,
        expires_at: datetime | None,
        expected_revision: int,
    ) -> dict[str, object] | None:
        result = await self._entitlements.add_storage_grant(
            user_id,
            quota_bytes=quota_bytes,
            expires_at=expires_at,
            expected_revision=expected_revision,
        )
        return await self.account_detail(user_id) if result is not None else None

    async def revoke_storage_grant(
        self, grant_id: UUID, *, expected_revision: int
    ) -> dict[str, object] | None:
        result = await self._entitlements.revoke_storage_grant(
            grant_id, expected_revision=expected_revision
        )
        return await self.account_detail(result[0].user_id) if result is not None else None

    async def grant_revision(self, grant_id: UUID) -> int | None:
        return await self._session.scalar(
            select(StorageQuotaGrant.revision).where(StorageQuotaGrant.id == grant_id)
        )

    async def reconcile(self, user_id: UUID, *, expected_revision: int) -> dict[str, object] | None:
        account = await self._entitlements.reconcile_storage(
            user_id, expected_revision=expected_revision
        )
        return await self.account_detail(user_id) if account is not None else None
