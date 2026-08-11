from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.infrastructure.database.models import User
from app.infrastructure.database.models.notification import (
    InAppNotification,
    NotificationJob,
    OutboxEvent,
    Reminder,
)
from app.infrastructure.database.session import init_database
from app.modules.notifications.worker import NotificationWorker
from sqlalchemy import select


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reminder_outbox_becomes_notification_job_and_in_app_notice() -> None:
    settings = Settings(notification_worker_enabled=False)
    if not settings.database_url:
        pytest.skip("MOMENT_ONE_DATABASE_URL is not configured")
    database = init_database(settings)
    user_id = uuid4()
    reminder_id = uuid4()
    now = datetime.now(UTC)
    try:
        async with database.session_factory() as session, session.begin():
            session.add(
                User(
                    id=user_id,
                    casdoor_sub=f"notification-worker-{user_id}",
                    casdoor_user_id=str(user_id),
                    display_name="Notification Worker Test",
                    status="active",
                    revision=1,
                )
            )
            session.add(
                Reminder(
                    id=reminder_id,
                    user_id=user_id,
                    title="测试提醒",
                    scene="general",
                    source_type="manual",
                    due_at=now - timedelta(seconds=1),
                    timezone="UTC",
                    status="pending",
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                OutboxEvent(
                    id=uuid4(),
                    event_type="reminder.created",
                    aggregate_type="reminder",
                    aggregate_id=str(reminder_id),
                    user_id=user_id,
                    aggregate_revision=1,
                    payload={
                        "dueAt": (now - timedelta(seconds=1)).isoformat(),
                        "status": "pending",
                    },
                    occurred_at=now,
                    available_at=now,
                )
            )

        events, jobs = await NotificationWorker(settings).run_once()
        assert events == 1
        assert jobs == 1

        async with database.session_factory() as session:
            event = await session.scalar(select(OutboxEvent).where(OutboxEvent.user_id == user_id))
            job = await session.scalar(
                select(NotificationJob).where(NotificationJob.user_id == user_id)
            )
            notification = await session.scalar(
                select(InAppNotification).where(InAppNotification.user_id == user_id)
            )
            assert event is not None and event.processed_at is not None
            assert job is not None and job.status == "sent"
            assert notification is not None and notification.body == "测试提醒"
    finally:
        async with database.session_factory() as session, session.begin():
            user = await session.get(User, user_id)
            if user is not None:
                await session.delete(user)
        await database.dispose()
