from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.notification import (
    InAppNotification,
    NotificationJob,
    NotificationPreference,
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


class NotificationCenterRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_preferences(self, user_id: UUID) -> NotificationPreference | None:
        return await self._session.get(NotificationPreference, user_id)

    async def save_preferences(self, preferences: NotificationPreference) -> NotificationPreference:
        self._session.add(preferences)
        await self._session.flush()
        return preferences

    async def list_notifications(
        self, user_id: UUID, *, unread_only: bool, limit: int
    ) -> list[InAppNotification]:
        query = select(InAppNotification).where(InAppNotification.user_id == user_id)
        if unread_only:
            query = query.where(InAppNotification.read_at.is_(None))
        result = await self._session.execute(
            query.order_by(InAppNotification.created_at.desc()).limit(limit)
        )
        return list(result.scalars())

    async def get_notification(
        self, notification_id: UUID, user_id: UUID
    ) -> InAppNotification | None:
        return await self._session.scalar(
            select(InAppNotification).where(
                InAppNotification.id == notification_id,
                InAppNotification.user_id == user_id,
            )
        )

    async def get_by_deduplication_key(self, deduplication_key: str) -> InAppNotification | None:
        return await self._session.scalar(
            select(InAppNotification).where(
                InAppNotification.deduplication_key == deduplication_key
            )
        )

    async def count_unread(self, user_id: UUID) -> int:
        return int(
            await self._session.scalar(
                select(func.count(InAppNotification.id)).where(
                    InAppNotification.user_id == user_id,
                    InAppNotification.read_at.is_(None),
                )
            )
            or 0
        )

    async def mark_all_read(self, user_id: UUID, *, read_at: datetime) -> int:
        result = await self._session.execute(
            update(InAppNotification)
            .where(
                InAppNotification.user_id == user_id,
                InAppNotification.read_at.is_(None),
            )
            .values(
                read_at=read_at,
                revision=InAppNotification.revision + 1,
            )
        )
        return int(result.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue]

    async def save_notification(self, notification: InAppNotification) -> None:
        self._session.add(notification)
        await self._session.flush()

    async def save_job(self, job: NotificationJob) -> None:
        self._session.add(job)
        await self._session.flush()
