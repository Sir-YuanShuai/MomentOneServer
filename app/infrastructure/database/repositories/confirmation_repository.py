"""PendingConfirmation 仓储：两阶段删除的票据持久化。

消费票据（mark_used）和 Moment 软删除必须在同一事务中完成，
调用方需在同一 AsyncSession 内顺序调用 mark_used → repo.soft_delete。
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import PendingConfirmation as PendingConfirmationORM


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """领域模型：删除确认票据。"""

    id: UUID
    user_id: UUID
    target_type: str
    target_id: UUID
    action: str
    expected_revision: int
    status: str
    preview: dict
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None


def _to_domain(orm: PendingConfirmationORM) -> PendingConfirmation:
    return PendingConfirmation(
        id=orm.id,
        user_id=orm.user_id,
        target_type=orm.target_type,
        target_id=orm.target_id,
        action=orm.action,
        expected_revision=orm.expected_revision,
        status=orm.status,
        preview=dict(orm.preview or {}),
        created_at=orm.created_at,
        expires_at=orm.expires_at,
        used_at=orm.used_at,
    )


class SqlConfirmationRepository:
    """pending_confirmations 表读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: UUID,
        target_type: str,
        target_id: UUID,
        action: str,
        expected_revision: int,
        preview: dict,
        expires_at: datetime,
    ) -> PendingConfirmation:
        orm = PendingConfirmationORM(
            id=uuid4(),
            user_id=user_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            expected_revision=expected_revision,
            status="pending",
            preview=preview,
            expires_at=expires_at,
        )
        self._session.add(orm)
        await self._session.flush()
        return _to_domain(orm)

    async def get(self, confirmation_id: UUID) -> PendingConfirmation | None:
        stmt = select(PendingConfirmationORM).where(PendingConfirmationORM.id == confirmation_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _to_domain(orm) if orm else None

    async def mark_used(self, *, confirmation_id: UUID, used_at: datetime) -> None:
        stmt = select(PendingConfirmationORM).where(PendingConfirmationORM.id == confirmation_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            return
        orm.status = "used"
        orm.used_at = used_at
        await self._session.flush()


__all__ = ["PendingConfirmation", "SqlConfirmationRepository"]
