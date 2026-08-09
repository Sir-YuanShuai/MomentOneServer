from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import (
    PlanDefinition,
    StorageQuotaGrant,
    UserEntitlement,
)
from app.modules.entitlements.repository import EntitlementRepository


class AdminPlanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_plans(self) -> list[dict[str, object]]:
        rows = list(
            (
                await self._session.execute(
                    select(PlanDefinition).order_by(PlanDefinition.created_at, PlanDefinition.key)
                )
            )
            .scalars()
            .all()
        )
        return [self.to_dict(item) for item in rows]

    async def create(
        self,
        *,
        key: str,
        name: str,
        status: str,
        entitlements: Mapping[str, bool],
        quotas: Mapping[str, int],
    ) -> dict[str, object] | None:
        existing = await self._session.scalar(
            select(PlanDefinition.key).where(PlanDefinition.key == key)
        )
        if existing is not None:
            return None
        plan = PlanDefinition(
            key=key,
            version=1,
            name=name,
            status=status,
            entitlements=dict(entitlements),
            quotas=dict(quotas),
        )
        self._session.add(plan)
        await self._session.flush()
        return self.to_dict(plan)

    async def update(
        self,
        *,
        key: str,
        expected_version: int,
        name: str | None,
        status: str | None,
        entitlements: Mapping[str, bool] | None,
        quotas: Mapping[str, int] | None,
    ) -> dict[str, object] | None:
        plan = await self._session.scalar(
            select(PlanDefinition).where(PlanDefinition.key == key).with_for_update()
        )
        if plan is None or plan.version != expected_version:
            return None
        if name is not None:
            plan.name = name
        if status is not None:
            plan.status = status
        if entitlements is not None:
            plan.entitlements = dict(entitlements)
        if quotas is not None:
            plan.quotas = dict(quotas)
        plan.version += 1
        plan.updated_at = datetime.now(UTC)
        await self._session.flush()
        await self._sync_active_subscribers(plan)
        return self.to_dict(plan)

    async def _sync_active_subscribers(self, plan: PlanDefinition) -> None:
        now = datetime.now(UTC)
        markers = list(
            (
                await self._session.execute(
                    select(UserEntitlement).where(
                        UserEntitlement.entitlement_key == f"plan:{plan.key}",
                        UserEntitlement.status == "active",
                        UserEntitlement.starts_at <= now,
                        (UserEntitlement.expires_at.is_(None) | (UserEntitlement.expires_at > now)),
                    )
                )
            )
            .scalars()
            .all()
        )
        desired_capabilities = {
            key for key, enabled in plan.entitlements.items() if enabled is True
        }
        storage_raw = plan.quotas.get("storage_bytes", 0)
        storage_bytes = storage_raw if isinstance(storage_raw, int) else 0
        entitlements = EntitlementRepository(self._session)
        for marker in markers:
            source_rows = list(
                (
                    await self._session.execute(
                        select(UserEntitlement).where(
                            UserEntitlement.user_id == marker.user_id,
                            UserEntitlement.source_type == marker.source_type,
                            UserEntitlement.source_ref == marker.source_ref,
                            UserEntitlement.status == "active",
                        )
                    )
                )
                .scalars()
                .all()
            )
            current_capabilities = {
                item.entitlement_key
                for item in source_rows
                if not item.entitlement_key.startswith("plan:")
            }
            for item in source_rows:
                if (
                    not item.entitlement_key.startswith("plan:")
                    and item.entitlement_key not in desired_capabilities
                ):
                    item.status = "revoked"
                    item.revision += 1
                    item.updated_at = now
            for capability in desired_capabilities - current_capabilities:
                self._session.add(
                    UserEntitlement(
                        id=uuid4(),
                        user_id=marker.user_id,
                        entitlement_key=capability,
                        source_type=marker.source_type,
                        source_ref=marker.source_ref,
                        status="active",
                        starts_at=marker.starts_at,
                        expires_at=marker.expires_at,
                        metadata_={"planKey": plan.key, "planVersion": plan.version},
                    )
                )
            grant = await self._session.scalar(
                select(StorageQuotaGrant).where(
                    StorageQuotaGrant.user_id == marker.user_id,
                    StorageQuotaGrant.source_type == marker.source_type,
                    StorageQuotaGrant.source_ref == marker.source_ref,
                    StorageQuotaGrant.status == "active",
                )
            )
            if grant is not None and grant.quota_bytes != storage_bytes:
                grant.quota_bytes = storage_bytes
                grant.revision += 1
                grant.updated_at = now
            account = await entitlements.locked_account(marker.user_id)
            await entitlements.reconcile_storage(marker.user_id, expected_revision=account.revision)
        await self._session.flush()

    async def version(self, key: str) -> int | None:
        return await self._session.scalar(
            select(PlanDefinition.version).where(PlanDefinition.key == key)
        )

    @staticmethod
    def to_dict(item: PlanDefinition) -> dict[str, object]:
        return {
            "key": item.key,
            "version": item.version,
            "name": item.name,
            "status": item.status,
            "entitlements": dict(item.entitlements or {}),
            "quotas": dict(item.quotas or {}),
            "createdAt": item.created_at.isoformat(),
            "updatedAt": item.updated_at.isoformat(),
        }


__all__ = ["AdminPlanRepository"]
