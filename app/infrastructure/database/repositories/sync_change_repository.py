"""离线同步增量拉取的变更日志仓储。"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.sync_change import SyncChange


class SqlSyncChangeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        user_id: UUID,
        entity_id: UUID,
        operation: str,
        revision: int,
        snapshot: dict,
    ) -> SyncChange:
        item = SyncChange(
            user_id=user_id,
            entity_type="moment",
            entity_id=entity_id,
            operation=operation,
            revision=revision,
            snapshot=snapshot,
        )
        self._session.add(item)
        await self._session.flush()
        return item

    async def list_after(self, *, user_id: UUID, sequence: int, limit: int) -> list[SyncChange]:
        result = await self._session.execute(
            select(SyncChange)
            .where(SyncChange.user_id == user_id, SyncChange.sequence > sequence)
            .order_by(SyncChange.sequence.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def latest_sequence(self, *, user_id: UUID) -> int:
        value = await self._session.scalar(
            select(func.max(SyncChange.sequence)).where(SyncChange.user_id == user_id)
        )
        return int(value or 0)


__all__ = ["SqlSyncChangeRepository"]
