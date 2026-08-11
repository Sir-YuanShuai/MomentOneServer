from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from app.core.errors import ApplicationError
from app.infrastructure.database.models.notification import OutboxEvent, Reminder
from app.infrastructure.database.repositories.notification_repository import (
    NotificationPipelineRepository,
    ReminderRepository,
)
from app.modules.notifications.reminders import ReminderService


class FakeReminderRepository:
    def __init__(self) -> None:
        self.items: dict[UUID, Reminder] = {}

    async def save(self, reminder: Reminder) -> Reminder:
        self.items[reminder.id] = reminder
        return reminder

    async def get(self, reminder_id: UUID, user_id: UUID) -> Reminder | None:
        item = self.items.get(reminder_id)
        return item if item is not None and item.user_id == user_id else None


class FakePipelineRepository:
    def __init__(self) -> None:
        self.events: list[OutboxEvent] = []

    async def emit(self, event: OutboxEvent) -> OutboxEvent:
        self.events.append(event)
        return event


def make_service() -> tuple[ReminderService, FakeReminderRepository, FakePipelineRepository]:
    reminders = FakeReminderRepository()
    pipeline = FakePipelineRepository()
    service = ReminderService(
        cast(ReminderRepository, reminders),
        cast(NotificationPipelineRepository, pipeline),
    )
    return service, reminders, pipeline


@pytest.mark.asyncio
async def test_create_reminder_emits_minimal_outbox_event() -> None:
    service, reminders, pipeline = make_service()
    user_id = uuid4()
    due_at = datetime.now(UTC) + timedelta(hours=1)

    created = await service.create(
        user_id=user_id,
        title="交水费",
        body="月底前处理",
        scene="general",
        due_at=due_at,
        timezone="Asia/Shanghai",
        correlation_id="request-1",
    )

    assert reminders.items[created.id] is created
    assert created.status == "pending"
    assert pipeline.events[0].event_type == "reminder.created"
    assert pipeline.events[0].aggregate_id == str(created.id)
    assert pipeline.events[0].payload == {"dueAt": due_at.isoformat(), "status": "pending"}


@pytest.mark.asyncio
async def test_create_reminder_rejects_past_time() -> None:
    service, _, pipeline = make_service()
    with pytest.raises(ApplicationError) as caught:
        await service.create(
            user_id=uuid4(),
            title="过期提醒",
            body=None,
            scene="general",
            due_at=datetime.now(UTC) - timedelta(minutes=1),
            timezone="UTC",
        )
    assert caught.value.code == "REMINDER_DUE_AT_INVALID"
    assert pipeline.events == []


@pytest.mark.asyncio
async def test_complete_reminder_checks_revision_and_emits_event() -> None:
    service, _, pipeline = make_service()
    user_id = uuid4()
    created = await service.create(
        user_id=user_id,
        title="交水费",
        body=None,
        scene="general",
        due_at=datetime.now(UTC) + timedelta(hours=1),
        timezone="UTC",
    )

    with pytest.raises(ApplicationError) as caught:
        await service.transition(
            user_id=user_id,
            reminder_id=created.id,
            expected_revision=99,
            target_status="completed",
        )
    assert caught.value.code == "REVISION_CONFLICT"

    completed = await service.transition(
        user_id=user_id,
        reminder_id=created.id,
        expected_revision=1,
        target_status="completed",
    )
    assert completed.status == "completed"
    assert completed.revision == 2
    assert pipeline.events[-1].event_type == "reminder.completed"

    reopened = await service.reopen(
        user_id=user_id,
        reminder_id=created.id,
        expected_revision=2,
    )
    assert reopened.status == "pending"
    assert reopened.completed_at is None
    assert reopened.revision == 3
    assert pipeline.events[-1].event_type == "reminder.rescheduled"
