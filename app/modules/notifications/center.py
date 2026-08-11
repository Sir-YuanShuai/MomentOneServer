from datetime import UTC, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.errors import ApplicationError
from app.infrastructure.database.models.notification import (
    InAppNotification,
    NotificationJob,
    NotificationPreference,
)
from app.infrastructure.database.repositories.notification_repository import (
    NotificationCenterRepository,
)


def serialize_preferences(item: NotificationPreference | None, user_id: UUID) -> dict:
    return {
        "userId": str(user_id),
        "webPushEnabled": item.web_push_enabled if item else True,
        "remindersEnabled": item.reminders_enabled if item else True,
        "habitEnabled": item.habit_enabled if item else True,
        "securityEnabled": item.security_enabled if item else True,
        "announcementsEnabled": item.announcements_enabled if item else True,
        "quietHoursEnabled": item.quiet_hours_enabled if item else False,
        "quietHoursStart": item.quiet_hours_start.isoformat()
        if item and item.quiet_hours_start
        else None,
        "quietHoursEnd": item.quiet_hours_end.isoformat()
        if item and item.quiet_hours_end
        else None,
        "timezone": item.timezone if item else "UTC",
        "lockScreenDetail": item.lock_screen_detail if item else "summary",
        "revision": item.revision if item else 0,
    }


def serialize_notification(item: InAppNotification) -> dict:
    return {
        "id": str(item.id),
        "category": item.category,
        "title": item.title,
        "body": item.body,
        "target": item.target,
        "readAt": item.read_at.isoformat() if item.read_at else None,
        "revision": item.revision,
        "createdAt": item.created_at.isoformat(),
    }


class NotificationCenterService:
    def __init__(self, repository: NotificationCenterRepository) -> None:
        self._repository = repository

    async def update_preferences(
        self,
        *,
        user_id: UUID,
        expected_revision: int,
        values: dict,
    ) -> NotificationPreference:
        item = await self._repository.get_preferences(user_id)
        current_revision = item.revision if item else 0
        if expected_revision != current_revision:
            raise ApplicationError(
                code="REVISION_CONFLICT",
                message="通知设置已在其他终端更新，请刷新后重试。",
                status_code=409,
                details={"currentRevision": current_revision},
            )
        timezone = values.get("timezone", item.timezone if item else "UTC")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ApplicationError(
                code="NOTIFICATION_TIMEZONE_INVALID",
                message="通知时区无效。",
                status_code=400,
            ) from exc
        if item is None:
            item = NotificationPreference(user_id=user_id)
        for key, value in values.items():
            setattr(item, key, value)
        item.timezone = timezone
        item.revision = current_revision + 1
        return await self._repository.save_preferences(item)

    async def mark_read(
        self, *, user_id: UUID, notification_id: UUID, expected_revision: int
    ) -> InAppNotification:
        item = await self._repository.get_notification(notification_id, user_id)
        if item is None:
            raise ApplicationError(
                code="NOTIFICATION_NOT_FOUND", message="通知不存在。", status_code=404
            )
        if item.revision != expected_revision:
            raise ApplicationError(
                code="REVISION_CONFLICT",
                message="通知状态已变化，请刷新后重试。",
                status_code=409,
                details={"currentRevision": item.revision},
            )
        if item.read_at is None:
            item.read_at = datetime.now(UTC)
            item.revision += 1
            await self._repository.save_notification(item)
        return item


async def enqueue_security_notification(
    repository: NotificationCenterRepository,
    *,
    user_id: UUID,
    title: str,
    body: str,
    target: str,
    event_key: str,
) -> InAppNotification:
    """在业务事务中创建安全通知及立即投递任务。"""
    notification = InAppNotification(
        id=uuid4(),
        user_id=user_id,
        category="security",
        title=title,
        body=body,
        target=target,
        tag=f"security-{event_key}",
        deduplication_key=f"security:{user_id}:{event_key}",
        source_type="security",
        source_id=event_key,
        revision=1,
    )
    await repository.save_notification(notification)
    await repository.save_job(
        NotificationJob(
            id=uuid4(),
            user_id=user_id,
            job_type="notification_delivery",
            source_type="notification",
            source_id=str(notification.id),
            source_revision=1,
            scheduled_at=datetime.now(UTC),
            status="pending",
            deduplication_key=f"delivery:{notification.deduplication_key}",
        )
    )
    return notification
