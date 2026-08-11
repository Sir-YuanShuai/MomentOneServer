from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import and_, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import HabitGoal as HabitGoalORM
from app.modules.habit_goals.domain import HabitGoal


def _orm_to_domain(orm: HabitGoalORM) -> HabitGoal:
    return HabitGoal(
        id=orm.id,
        user_id=orm.user_id,
        name=orm.name,
        unit=orm.unit,
        frequency=orm.frequency,
        times_per_week=orm.times_per_week,
        color=orm.color,
        revision=orm.revision,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
        deleted_at=orm.deleted_at,
    )


class SqlHabitGoalRepository:
    """PostgreSQL-backed HabitGoal repository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, goal_id: UUID, user_id: UUID) -> HabitGoal | None:
        stmt = select(HabitGoalORM).where(
            and_(
                HabitGoalORM.id == goal_id,
                HabitGoalORM.user_id == user_id,
                HabitGoalORM.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _orm_to_domain(orm) if orm else None

    async def list_by_user(self, user_id: UUID) -> list[HabitGoal]:
        stmt = (
            select(HabitGoalORM)
            .where(
                and_(
                    HabitGoalORM.user_id == user_id,
                    HabitGoalORM.deleted_at.is_(None),
                )
            )
            .order_by(HabitGoalORM.created_at.desc(), HabitGoalORM.id.desc())
        )
        result = await self._session.execute(stmt)
        return [_orm_to_domain(orm) for orm in result.scalars().all()]

    async def count_by_user(self, user_id: UUID) -> int:
        result = await self._session.scalar(
            select(func.count())
            .select_from(HabitGoalORM)
            .where(HabitGoalORM.user_id == user_id, HabitGoalORM.deleted_at.is_(None))
        )
        return int(result or 0)

    async def soft_delete_all(self, user_id: UUID) -> int:
        result = await self._session.execute(
            update(HabitGoalORM)
            .where(HabitGoalORM.user_id == user_id, HabitGoalORM.deleted_at.is_(None))
            .values(deleted_at=datetime.now(UTC), revision=HabitGoalORM.revision + 1)
        )
        return int(cast(CursorResult, result).rowcount or 0)

    async def create(self, goal: HabitGoal) -> HabitGoal:
        orm = HabitGoalORM(
            id=goal.id,
            user_id=goal.user_id,
            name=goal.name,
            unit=goal.unit,
            frequency=goal.frequency,
            times_per_week=goal.times_per_week,
            color=goal.color,
            revision=goal.revision,
        )
        self._session.add(orm)
        await self._session.flush()
        return _orm_to_domain(orm)

    async def update(self, goal_id: UUID, user_id: UUID, **fields) -> HabitGoal | None:
        stmt = select(HabitGoalORM).where(
            and_(
                HabitGoalORM.id == goal_id,
                HabitGoalORM.user_id == user_id,
                HabitGoalORM.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if not orm:
            return None
        if "name" in fields:
            orm.name = fields["name"]
        if "unit" in fields:
            orm.unit = fields["unit"]
        if "frequency" in fields:
            orm.frequency = fields["frequency"]
        if "times_per_week" in fields:
            orm.times_per_week = fields["times_per_week"]
        if "color" in fields:
            orm.color = fields["color"]
        orm.revision += 1
        await self._session.flush()
        await self._session.refresh(orm)
        return _orm_to_domain(orm)

    async def soft_delete(self, goal_id: UUID, user_id: UUID) -> HabitGoal | None:
        stmt = select(HabitGoalORM).where(
            and_(
                HabitGoalORM.id == goal_id,
                HabitGoalORM.user_id == user_id,
                HabitGoalORM.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if not orm:
            return None
        orm.deleted_at = datetime.now(UTC)
        orm.revision += 1
        await self._session.flush()
        await self._session.refresh(orm)
        return _orm_to_domain(orm)
