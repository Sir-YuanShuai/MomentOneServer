from dataclasses import dataclass

from app.infrastructure.database.models.notification import NotificationPreference


@dataclass(frozen=True, slots=True)
class NotificationDeliveryPolicy:
    """通知类别到系统 Push 行为的唯一映射。站内通知始终先创建。"""

    preference_field: str
    push_summary: str
    respect_quiet_hours: bool


_POLICIES = {
    "reminder": NotificationDeliveryPolicy(
        preference_field="reminders_enabled",
        push_summary="你有一项待处理提醒",
        respect_quiet_hours=True,
    ),
    "habit": NotificationDeliveryPolicy(
        preference_field="habit_enabled",
        push_summary="有一项习惯等待完成",
        respect_quiet_hours=True,
    ),
    "security": NotificationDeliveryPolicy(
        preference_field="security_enabled",
        push_summary="账号安全状态有更新",
        respect_quiet_hours=False,
    ),
    "announcement": NotificationDeliveryPolicy(
        preference_field="announcements_enabled",
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
        return True
    return preference.web_push_enabled and bool(getattr(preference, policy.preference_field))
