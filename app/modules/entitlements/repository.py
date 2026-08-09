from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApplicationError
from app.infrastructure.database.models import (
    Asset,
    PlanDefinition,
    StorageQuotaGrant,
    UserEntitlement,
    UserStorageAccount,
)

GIB = 1024 * 1024 * 1024
DEFAULT_PLAN_KEY = "free"
DEFAULT_STORAGE_BYTES = GIB


def _active_at(model, now: datetime):
    return and_(
        model.status == "active",
        model.starts_at <= now,
        or_(model.expires_at.is_(None), model.expires_at > now),
    )


class EntitlementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_plans(self, *, active_only: bool = True) -> list[PlanDefinition]:
        stmt = select(PlanDefinition).order_by(
            PlanDefinition.quotas["storage_bytes"].astext.cast(int)
        )
        if active_only:
            stmt = stmt.where(PlanDefinition.status == "active")
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_plan(self, plan_key: str, *, active_only: bool = True) -> PlanDefinition | None:
        stmt = select(PlanDefinition).where(PlanDefinition.key == plan_key)
        if active_only:
            stmt = stmt.where(PlanDefinition.status == "active")
        return await self._session.scalar(stmt)

    async def ensure_user_defaults(self, user_id: UUID) -> UserStorageAccount:
        account = await self._session.scalar(
            select(UserStorageAccount).where(UserStorageAccount.user_id == user_id)
        )
        if account is not None:
            return account
        now = datetime.now(UTC)
        for entitlement_key in ("plan:free", "moment.core", "media.upload"):
            self._session.add(
                UserEntitlement(
                    id=uuid4(),
                    user_id=user_id,
                    entitlement_key=entitlement_key,
                    source_type="default",
                    source_ref="plan:free",
                    status="active",
                    starts_at=now,
                    metadata_={"planKey": DEFAULT_PLAN_KEY},
                )
            )
        self._session.add(
            StorageQuotaGrant(
                id=uuid4(),
                user_id=user_id,
                source_type="default",
                source_ref="plan:free",
                quota_bytes=DEFAULT_STORAGE_BYTES,
                status="active",
                starts_at=now,
            )
        )
        account = UserStorageAccount(
            user_id=user_id,
            used_bytes=0,
            reserved_bytes=0,
            effective_quota_bytes=DEFAULT_STORAGE_BYTES,
            over_quota=False,
            revision=1,
        )
        self._session.add(account)
        await self._session.flush()
        return account

    async def locked_account(self, user_id: UUID) -> UserStorageAccount:
        await self.ensure_user_defaults(user_id)
        account = await self._session.scalar(
            select(UserStorageAccount)
            .where(UserStorageAccount.user_id == user_id)
            .with_for_update()
        )
        if account is None:  # pragma: no cover - protected by ensure_user_defaults
            raise RuntimeError("storage account provisioning failed")
        now = datetime.now(UTC)
        active_quota = await self._session.scalar(
            select(func.sum(StorageQuotaGrant.quota_bytes)).where(
                StorageQuotaGrant.user_id == user_id,
                _active_at(StorageQuotaGrant, now),
            )
        )
        effective_quota = int(active_quota or 0)
        over_quota = account.used_bytes + account.reserved_bytes > effective_quota
        if account.effective_quota_bytes != effective_quota or account.over_quota != over_quota:
            account.effective_quota_bytes = effective_quota
            account.over_quota = over_quota
            account.revision += 1
            await self._session.flush()
        return account

    async def reserve_upload(self, user_id: UUID, size_bytes: int) -> UserStorageAccount:
        account = await self.locked_account(user_id)
        requested_total = account.used_bytes + account.reserved_bytes + size_bytes
        if account.over_quota or requested_total > account.effective_quota_bytes:
            raise ApplicationError(
                code="STORAGE_QUOTA_EXCEEDED",
                message="存储空间不足，请清理文件或升级套餐。",
                status_code=409,
                details={
                    "usedBytes": account.used_bytes,
                    "reservedBytes": account.reserved_bytes,
                    "effectiveQuotaBytes": account.effective_quota_bytes,
                    "requestedBytes": size_bytes,
                },
            )
        account.reserved_bytes += size_bytes
        account.revision += 1
        account.over_quota = (
            account.used_bytes + account.reserved_bytes > account.effective_quota_bytes
        )
        await self._session.flush()
        return account

    async def complete_upload(
        self, user_id: UUID, *, reserved_bytes: int, actual_bytes: int
    ) -> UserStorageAccount:
        account = await self.locked_account(user_id)
        account.reserved_bytes = max(0, account.reserved_bytes - max(0, reserved_bytes))
        account.used_bytes += max(0, actual_bytes)
        account.revision += 1
        account.over_quota = (
            account.used_bytes + account.reserved_bytes > account.effective_quota_bytes
        )
        await self._session.flush()
        return account

    async def release_upload(self, user_id: UUID, *, reserved_bytes: int) -> UserStorageAccount:
        account = await self.locked_account(user_id)
        account.reserved_bytes = max(0, account.reserved_bytes - max(0, reserved_bytes))
        account.revision += 1
        account.over_quota = (
            account.used_bytes + account.reserved_bytes > account.effective_quota_bytes
        )
        await self._session.flush()
        return account

    async def max_upload_bytes(self, user_id: UUID) -> int | None:
        plan = await self.get_plan(await self.current_plan_key(user_id), active_only=False)
        if plan is None:
            return None
        raw = plan.quotas.get("max_upload_bytes")
        return raw if isinstance(raw, int) and raw > 0 else None

    async def current_plan_key(self, user_id: UUID) -> str:
        now = datetime.now(UTC)
        entitlement = await self._session.scalar(
            select(UserEntitlement)
            .where(
                UserEntitlement.user_id == user_id,
                UserEntitlement.entitlement_key.like("plan:%"),
                _active_at(UserEntitlement, now),
            )
            .order_by(UserEntitlement.starts_at.desc())
            .limit(1)
        )
        if entitlement is None:
            return DEFAULT_PLAN_KEY
        return entitlement.entitlement_key.split(":", 1)[1]

    async def list_user_entitlements(self, user_id: UUID) -> list[UserEntitlement]:
        await self.ensure_user_defaults(user_id)
        return list(
            (
                await self._session.execute(
                    select(UserEntitlement)
                    .where(UserEntitlement.user_id == user_id)
                    .order_by(UserEntitlement.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

    async def list_storage_grants(self, user_id: UUID) -> list[StorageQuotaGrant]:
        await self.ensure_user_defaults(user_id)
        return list(
            (
                await self._session.execute(
                    select(StorageQuotaGrant)
                    .where(StorageQuotaGrant.user_id == user_id)
                    .order_by(StorageQuotaGrant.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

    async def _recalculate_effective_quota(
        self, account: UserStorageAccount, *, reconciled: bool = False
    ) -> UserStorageAccount:
        now = datetime.now(UTC)
        total = await self._session.scalar(
            select(func.sum(StorageQuotaGrant.quota_bytes)).where(
                StorageQuotaGrant.user_id == account.user_id,
                _active_at(StorageQuotaGrant, now),
            )
        )
        account.effective_quota_bytes = int(total or 0)
        account.over_quota = (
            account.used_bytes + account.reserved_bytes > account.effective_quota_bytes
        )
        account.revision += 1
        if reconciled:
            account.reconciled_at = now
        await self._session.flush()
        return account

    async def set_plan(
        self, user_id: UUID, *, plan_key: str, expected_revision: int
    ) -> UserStorageAccount | None:
        plan = await self.get_plan(plan_key)
        if plan is None:
            raise ApplicationError(
                code="PLAN_NOT_FOUND", message="套餐不存在或已停用。", status_code=404
            )
        account = await self.locked_account(user_id)
        if account.revision != expected_revision:
            return None
        now = datetime.now(UTC)
        entitlements = list(
            (
                await self._session.execute(
                    select(UserEntitlement).where(
                        UserEntitlement.user_id == user_id,
                        UserEntitlement.source_ref.like("plan:%"),
                        UserEntitlement.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        for item in entitlements:
            item.status = "revoked"
            item.revision += 1
            item.updated_at = now
        plan_grants = list(
            (
                await self._session.execute(
                    select(StorageQuotaGrant).where(
                        StorageQuotaGrant.user_id == user_id,
                        StorageQuotaGrant.source_ref.like("plan:%"),
                        StorageQuotaGrant.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        for item in plan_grants:
            item.status = "revoked"
            item.revision += 1
            item.updated_at = now
        source_ref = f"plan:{plan_key}:{uuid4()}"
        entitlement_keys = [f"plan:{plan_key}"]
        entitlement_keys.extend(
            key for key, enabled in plan.entitlements.items() if enabled is True
        )
        for entitlement_key in entitlement_keys:
            self._session.add(
                UserEntitlement(
                    user_id=user_id,
                    entitlement_key=entitlement_key,
                    source_type="admin",
                    source_ref=source_ref,
                    status="active",
                    starts_at=now,
                    metadata_={"planKey": plan_key, "planVersion": plan.version},
                )
            )
        raw_storage_bytes = plan.quotas.get("storage_bytes", 0)
        storage_bytes = raw_storage_bytes if isinstance(raw_storage_bytes, int) else 0
        self._session.add(
            StorageQuotaGrant(
                user_id=user_id,
                source_type="admin",
                source_ref=source_ref,
                quota_bytes=storage_bytes,
                status="active",
                starts_at=now,
            )
        )
        await self._session.flush()
        return await self._recalculate_effective_quota(account)

    async def add_storage_grant(
        self,
        user_id: UUID,
        *,
        quota_bytes: int,
        expires_at: datetime | None,
        expected_revision: int,
    ) -> tuple[UserStorageAccount, StorageQuotaGrant] | None:
        account = await self.locked_account(user_id)
        if account.revision != expected_revision:
            return None
        grant = StorageQuotaGrant(
            user_id=user_id,
            source_type="admin",
            source_ref=f"bonus:{uuid4()}",
            quota_bytes=quota_bytes,
            status="active",
            starts_at=datetime.now(UTC),
            expires_at=expires_at,
        )
        self._session.add(grant)
        await self._session.flush()
        await self._recalculate_effective_quota(account)
        return account, grant

    async def revoke_storage_grant(
        self, grant_id: UUID, *, expected_revision: int
    ) -> tuple[UserStorageAccount, StorageQuotaGrant] | None:
        grant = await self._session.scalar(
            select(StorageQuotaGrant).where(StorageQuotaGrant.id == grant_id).with_for_update()
        )
        if grant is None or grant.revision != expected_revision or grant.status != "active":
            return None
        if grant.source_ref.startswith("plan:"):
            raise ApplicationError(
                code="PLAN_GRANT_REQUIRED",
                message="套餐基础额度不能单独撤销，请变更用户套餐。",
                status_code=409,
            )
        grant.status = "revoked"
        grant.revision += 1
        grant.updated_at = datetime.now(UTC)
        account = await self.locked_account(grant.user_id)
        await self._recalculate_effective_quota(account)
        return account, grant

    async def reconcile_storage(
        self, user_id: UUID, *, expected_revision: int
    ) -> UserStorageAccount | None:
        account = await self.locked_account(user_id)
        if account.revision != expected_revision:
            return None
        used = await self._session.scalar(
            select(func.sum(Asset.size_bytes)).where(
                Asset.user_id == user_id, Asset.state == "ready", Asset.deleted_at.is_(None)
            )
        )
        reserved = await self._session.scalar(
            select(func.sum(Asset.size_bytes)).where(
                Asset.user_id == user_id, Asset.state == "uploading", Asset.deleted_at.is_(None)
            )
        )
        account.used_bytes = int(used or 0)
        account.reserved_bytes = int(reserved or 0)
        return await self._recalculate_effective_quota(account, reconciled=True)
