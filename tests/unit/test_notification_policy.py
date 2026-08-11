from datetime import time

from app.infrastructure.database.models.notification import NotificationPreference
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
