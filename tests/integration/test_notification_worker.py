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
from app.infrastructure.database.repositories.notification_repository import (
    NotificationCenterRepository,
)
from app.infrastructure.database.session import init_database
from app.modules.notifications.center import (
    NotificationCenterService,
    enqueue_security_notification,
)
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
                        "remindAt": (now - timedelta(seconds=1)).isoformat(),
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
            assert notification is not None and notification.title == "测试提醒"
            assert notification.body == "现在该处理这项提醒了。"
    finally:
        async with database.session_factory() as session, session.begin():
            user = await session.get(User, user_id)
            if user is not None:
                await session.delete(user)
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_security_notification_is_delivered_without_creating_reminder() -> None:
    settings = Settings(notification_worker_enabled=False)
    if not settings.database_url:
        pytest.skip("MOMENT_ONE_DATABASE_URL is not configured")
    database = init_database(settings)
    user_id = uuid4()
    try:
        async with database.session_factory() as session, session.begin():
            session.add(
                User(
                    id=user_id,
                    casdoor_sub=f"security-notification-{user_id}",
                    casdoor_user_id=str(user_id),
                    display_name="Security Notification Test",
                    status="active",
                    revision=1,
                )
            )
            await session.flush()
            notification = await enqueue_security_notification(
                NotificationCenterRepository(session),
                user_id=user_id,
                title="新增 MCP 授权",
                body="测试客户端已获得授权。",
                target="/space/settings/?section=mcp-connections",
                event_key=f"test-{uuid4()}",
            )

        events, jobs = await NotificationWorker(settings).run_once()
        assert events == 0
        assert jobs == 1

        async with database.session_factory() as session:
            job = await session.scalar(
                select(NotificationJob).where(NotificationJob.user_id == user_id)
            )
            stored = await session.get(InAppNotification, notification.id)
            assert job is not None and job.status == "sent"
            assert stored is not None and stored.category == "security"

        async with database.session_factory() as session, session.begin():
            repository = NotificationCenterRepository(session)
            updated = await NotificationCenterService(repository).mark_all_read(user_id=user_id)
            assert updated == 1
            assert await repository.count_unread(user_id) == 0
    finally:
        async with database.session_factory() as session, session.begin():
            user = await session.get(User, user_id)
            if user is not None:
                await session.delete(user)
        await database.dispose()
