from dataclasses import dataclass

from app.infrastructure.database.models.notification import NotificationPreference


@dataclass(frozen=True, slots=True)
class NotificationDeliveryPolicy:
    """通知类别到投递偏好的唯一映射。"""

    preference_field: str
    channel_field: str
    push_summary: str
    respect_quiet_hours: bool


_POLICIES = {
    "reminder": NotificationDeliveryPolicy(
        preference_field="reminders_enabled",
        channel_field="reminder_channel",
        push_summary="你有一项待处理提醒",
        respect_quiet_hours=True,
    ),
    "habit": NotificationDeliveryPolicy(
        preference_field="habit_enabled",
        channel_field="habit_channel",
        push_summary="有一项习惯等待完成",
        respect_quiet_hours=True,
    ),
    "security": NotificationDeliveryPolicy(
        preference_field="security_enabled",
        channel_field="security_channel",
        push_summary="账号安全状态有更新",
        respect_quiet_hours=False,
    ),
    "announcement": NotificationDeliveryPolicy(
        preference_field="announcements_enabled",
        channel_field="announcement_channel",
        push_summary="Moment One 有一条重要消息",
        respect_quiet_hours=True,
    ),
}


def delivery_policy(category: str) -> NotificationDeliveryPolicy:
    try:
        return _POLICIES[category]
    except KeyError as exc:
        raise ValueError(f"Unsupported notification category: {category}") from exc


def system_push_allowed(
    policy: NotificationDeliveryPolicy,
    preference: NotificationPreference | None,
) -> bool:
    if preference is None:
        return False
    return preference.web_push_enabled and getattr(preference, policy.channel_field) == "system"


def in_app_allowed(
    policy: NotificationDeliveryPolicy,
    preference: NotificationPreference | None,
) -> bool:
    if preference is None:
        return policy.channel_field != "announcement_channel"
    return getattr(preference, policy.channel_field) != "off"
