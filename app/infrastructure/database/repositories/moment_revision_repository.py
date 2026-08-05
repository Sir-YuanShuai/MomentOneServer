"""MomentRevision 仓储：每次成功变更后的完整业务快照。

调用方负责构造 snapshot（领域字段，不含短期 URL/Token）。
"""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import MomentRevision as MomentRevisionORM


class SqlMomentRevisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        user_id: UUID,
        moment_id: UUID,
        revision: int,
        operation: str,
        snapshot: dict,
        actor_user_id: UUID | None = None,
    ) -> None:
        orm = MomentRevisionORM(
            id=uuid4(),
            user_id=user_id,
            moment_id=moment_id,
            revision=revision,
            operation=operation,
            snapshot=snapshot,
            actor_user_id=actor_user_id,
        )
        self._session.add(orm)
        await self._session.flush()


__all__ = ["SqlMomentRevisionRepository"]
