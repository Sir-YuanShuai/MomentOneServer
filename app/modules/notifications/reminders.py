from datetime import UTC, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.errors import ApplicationError
from app.infrastructure.database.models.notification import OutboxEvent, Reminder
from app.infrastructure.database.repositories.notification_repository import (
    NotificationPipelineRepository,
    ReminderRepository,
)


def serialize_reminder(item: Reminder) -> dict:
    return {
        "id": str(item.id),
        "title": item.title,
        "note": item.body or "",
        "scene": item.scene,
        "sourceType": item.source_type,
        "sourceId": item.source_id,
        "dueAt": item.due_at.isoformat(),
        "timezone": item.timezone,
        "status": item.status,
        "revision": item.revision,
        "completedAt": item.completed_at.isoformat() if item.completed_at else None,
        "cancelledAt": item.cancelled_at.isoformat() if item.cancelled_at else None,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
    }


class ReminderService:
    def __init__(
        self,
        reminders: ReminderRepository,
        pipeline: NotificationPipelineRepository,
    ) -> None:
        self._reminders = reminders
        self._pipeline = pipeline

    @staticmethod
    def validate_timezone(value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ApplicationError(
                code="REMINDER_TIMEZONE_INVALID",
                message="提醒时区不是有效的 IANA 时区。",
                status_code=400,
            ) from exc
        return value

    async def create(
        self,
        *,
        user_id: UUID,
        title: str,
        body: str | None,
        scene: str,
        due_at: datetime,
        timezone: str,
        source_type: str = "manual",
        source_id: str | None = None,
        correlation_id: str | None = None,
    ) -> Reminder:
        now = datetime.now(UTC)
        if due_at.tzinfo is None or due_at <= now:
            raise ApplicationError(
                code="REMINDER_DUE_AT_INVALID",
                message="提醒时间必须是带时区的未来时间。",
                status_code=400,
            )
        item = Reminder(
            id=uuid4(),
            user_id=user_id,
            title=title.strip(),
            body=body.strip() if body else None,
            scene=scene,
            source_type=source_type,
            source_id=source_id,
            due_at=due_at,
            timezone=self.validate_timezone(timezone),
            status="pending",
            revision=1,
            created_at=now,
            updated_at=now,
        )
        await self._reminders.save(item)
        await self._emit(item, "reminder.created", correlation_id)
        return item

    async def get(self, *, user_id: UUID, reminder_id: UUID) -> Reminder:
        item = await self._reminders.get(reminder_id, user_id)
        if item is None:
            raise ApplicationError(
                code="REMINDER_NOT_FOUND", message="提醒不存在。", status_code=404
            )
        return item

    async def update(
        self,
        *,
        user_id: UUID,
        reminder_id: UUID,
        expected_revision: int,
        title: str | None = None,
        body: str | None = None,
        scene: str | None = None,
        due_at: datetime | None = None,
        timezone: str | None = None,
        correlation_id: str | None = None,
    ) -> Reminder:
        item = await self.get(user_id=user_id, reminder_id=reminder_id)
        self.assert_revision(item, expected_revision)
        if item.status != "pending":
            raise ApplicationError(
                code="REMINDER_NOT_PENDING",
                message="只有待处理提醒可以修改。",
                status_code=409,
            )
        if due_at is not None:
            if due_at.tzinfo is None or due_at <= datetime.now(UTC):
                raise ApplicationError(
                    code="REMINDER_DUE_AT_INVALID",
                    message="提醒时间必须是带时区的未来时间。",
                    status_code=400,
                )
            item.due_at = due_at
        if title is not None:
            item.title = title.strip()
        if body is not None:
            item.body = body.strip() or None
        if scene is not None:
            item.scene = scene
        if timezone is not None:
            item.timezone = self.validate_timezone(timezone)
        item.revision += 1
        item.updated_at = datetime.now(UTC)
        await self._reminders.save(item)
        await self._emit(item, "reminder.rescheduled", correlation_id)
        return item

    async def transition(
        self,
        *,
        user_id: UUID,
        reminder_id: UUID,
        expected_revision: int,
        target_status: str,
        correlation_id: str | None = None,
    ) -> Reminder:
        item = await self.get(user_id=user_id, reminder_id=reminder_id)
        self.assert_revision(item, expected_revision)
        if item.status != "pending":
            raise ApplicationError(
                code="REMINDER_NOT_PENDING",
                message="该提醒已经处理。",
                status_code=409,
            )
        now = datetime.now(UTC)
        item.status = target_status
        item.revision += 1
        item.updated_at = now
        if target_status == "completed":
            item.completed_at = now
            event_type = "reminder.completed"
        else:
            item.cancelled_at = now
            event_type = "reminder.cancelled"
        await self._reminders.save(item)
        await self._emit(item, event_type, correlation_id)
        return item

    async def reopen(
        self,
        *,
        user_id: UUID,
        reminder_id: UUID,
        expected_revision: int,
        correlation_id: str | None = None,
    ) -> Reminder:
        item = await self.get(user_id=user_id, reminder_id=reminder_id)
        self.assert_revision(item, expected_revision)
        if item.status != "completed":
            raise ApplicationError(
                code="REMINDER_NOT_COMPLETED",
                message="只有已完成提醒可以恢复。",
                status_code=409,
            )
        item.status = "pending"
        item.completed_at = None
        item.revision += 1
        item.updated_at = datetime.now(UTC)
        await self._reminders.save(item)
        await self._emit(item, "reminder.rescheduled", correlation_id)
        return item

    async def delete(
        self,
        *,
        user_id: UUID,
        reminder_id: UUID,
        expected_revision: int,
        correlation_id: str | None = None,
    ) -> Reminder:
        item = await self.get(user_id=user_id, reminder_id=reminder_id)
        self.assert_revision(item, expected_revision)
        await self._reminders.soft_delete(item)
        await self._emit(item, "reminder.deleted", correlation_id)
        return item

    @staticmethod
    def assert_revision(item: Reminder, expected: int) -> None:
        if item.revision != expected:
            raise ApplicationError(
                code="REVISION_CONFLICT",
                message="提醒已在其他终端发生变化，请刷新后重试。",
                status_code=409,
                details={"currentRevision": item.revision},
            )

    async def _emit(self, item: Reminder, event_type: str, correlation_id: str | None) -> None:
        await self._pipeline.emit(
            OutboxEvent(
                id=uuid4(),
                event_type=event_type,
                aggregate_type="reminder",
                aggregate_id=str(item.id),
                user_id=item.user_id,
                aggregate_revision=item.revision,
                payload={"dueAt": item.due_at.isoformat(), "status": item.status},
                correlation_id=correlation_id,
                occurred_at=datetime.now(UTC),
                available_at=datetime.now(UTC),
            )
        )
