from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.notification import (
    NotificationJob,
    OutboxEvent,
    Reminder,
)


class ReminderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_user(self, user_id: UUID) -> list[Reminder]:
        result = await self._session.execute(
            select(Reminder)
            .where(Reminder.user_id == user_id, Reminder.deleted_at.is_(None))
            .order_by(Reminder.due_at.asc(), Reminder.created_at.desc())
        )
        return list(result.scalars())

    async def get(self, reminder_id: UUID, user_id: UUID) -> Reminder | None:
        result = await self._session.execute(
            select(Reminder).where(
                Reminder.id == reminder_id,
                Reminder.user_id == user_id,
                Reminder.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def save(self, reminder: Reminder) -> Reminder:
        self._session.add(reminder)
        await self._session.flush()
        return reminder

    async def soft_delete(self, reminder: Reminder) -> Reminder:
        deleted_at = datetime.now(UTC)
        reminder.deleted_at = deleted_at
        reminder.revision += 1
        reminder.updated_at = deleted_at
        self._session.add(reminder)
        await self._session.flush()
        return reminder


class NotificationPipelineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def emit(self, event: OutboxEvent) -> OutboxEvent:
        self._session.add(event)
        await self._session.flush()
        return event

    async def cancel_pending_jobs(self, *, source_type: str, source_id: str) -> None:
        await self._session.execute(
            update(NotificationJob)
            .where(
                NotificationJob.source_type == source_type,
                NotificationJob.source_id == source_id,
                NotificationJob.status.in_(["pending", "retry"]),
            )
            .values(
                status="cancelled", locked_at=None, locked_by=None, updated_at=datetime.now(UTC)
            )
        )
