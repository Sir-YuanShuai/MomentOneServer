from datetime import time
from uuid import uuid4

from app.infrastructure.database.models.notification import (
    NotificationDelivery,
    NotificationPreference,
)
from app.modules.notifications.policy import delivery_policy, system_push_allowed


def test_security_push_bypasses_quiet_hours() -> None:
    policy = delivery_policy("security")

    assert policy.respect_quiet_hours is False
    assert policy.preference_field == "security_enabled"


def test_reminder_and_habit_push_respect_quiet_hours() -> None:
    assert delivery_policy("reminder").respect_quiet_hours is True
    assert delivery_policy("habit").respect_quiet_hours is True


def test_category_and_global_preferences_both_control_system_push() -> None:
    preference = NotificationPreference(
        web_push_enabled=True,
        reminders_enabled=False,
        quiet_hours_enabled=True,
        quiet_hours_start=time(23, 0),
        quiet_hours_end=time(7, 0),
    )

    assert system_push_allowed(delivery_policy("reminder"), preference) is False
    preference.reminders_enabled = True
    assert system_push_allowed(delivery_policy("reminder"), preference) is True
    preference.web_push_enabled = False
    assert system_push_allowed(delivery_policy("reminder"), preference) is False


def test_new_delivery_attempt_count_is_initialized_before_flush() -> None:
    delivery = NotificationDelivery(
        id=uuid4(),
        notification_id=uuid4(),
        user_id=uuid4(),
        channel="web_push",
        target_id=uuid4(),
        status="pending",
        attempt_count=0,
    )

    delivery.attempt_count = (delivery.attempt_count or 0) + 1

    assert delivery.attempt_count == 1
