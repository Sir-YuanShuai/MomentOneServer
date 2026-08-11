from datetime import time
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth_context
from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.notification_repository import (
    NotificationCenterRepository,
)
from app.infrastructure.database.session import get_db_session
from app.modules.notifications.center import (
    NotificationCenterService,
    serialize_notification,
    serialize_preferences,
)

router = APIRouter(tags=["notifications"])


class NotificationPreferenceUpdateRequest(BaseModel):
    expectedRevision: int = Field(ge=0)
    webPushEnabled: bool | None = None
    remindersEnabled: bool | None = None
    habitEnabled: bool | None = None
    securityEnabled: bool | None = None
    announcementsEnabled: bool | None = None
    quietHoursEnabled: bool | None = None
    quietHoursStart: time | None = None
    quietHoursEnd: time | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    lockScreenDetail: str | None = Field(default=None, pattern="^(summary|full)$")


class MarkReadRequest(BaseModel):
    expectedRevision: int = Field(ge=1)


PREFERENCE_FIELDS = {
    "webPushEnabled": "web_push_enabled",
    "remindersEnabled": "reminders_enabled",
    "habitEnabled": "habit_enabled",
    "securityEnabled": "security_enabled",
    "announcementsEnabled": "announcements_enabled",
    "quietHoursEnabled": "quiet_hours_enabled",
    "quietHoursStart": "quiet_hours_start",
    "quietHoursEnd": "quiet_hours_end",
    "timezone": "timezone",
    "lockScreenDetail": "lock_screen_detail",
}


@router.get("/v1/notification-preferences")
async def get_notification_preferences(
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    item = await NotificationCenterRepository(session).get_preferences(ctx.user_id)
    return serialize_preferences(item, ctx.user_id)


@router.patch("/v1/notification-preferences")
async def update_notification_preferences(
    body: NotificationPreferenceUpdateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    if not idempotency_key:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED", message="缺少幂等键。", status_code=400
        )
    raw = body.model_dump(exclude={"expectedRevision"}, exclude_unset=True)
    values = {PREFERENCE_FIELDS[key]: value for key, value in raw.items() if value is not None}
    repository = NotificationCenterRepository(session)
    item = await NotificationCenterService(repository).update_preferences(
        user_id=ctx.user_id,
        expected_revision=body.expectedRevision,
        values=values,
    )
    return serialize_preferences(item, ctx.user_id)


@router.get("/v1/notifications")
async def list_notifications(
    unread_only: bool = Query(default=False, alias="unreadOnly"),
    limit: int = Query(default=50, ge=1, le=100),
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    repository = NotificationCenterRepository(session)
    items = await repository.list_notifications(ctx.user_id, unread_only=unread_only, limit=limit)
    return {
        "items": [serialize_notification(item) for item in items],
        "unreadCount": await repository.count_unread(ctx.user_id),
    }


@router.post("/v1/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: UUID,
    body: MarkReadRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    if not idempotency_key:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED", message="缺少幂等键。", status_code=400
        )
    item = await NotificationCenterService(NotificationCenterRepository(session)).mark_read(
        user_id=ctx.user_id,
        notification_id=notification_id,
        expected_revision=body.expectedRevision,
    )
    return serialize_notification(item)


@router.post("/v1/notifications/read-all")
async def mark_all_notifications_read(
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    if not idempotency_key:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED", message="缺少幂等键。", status_code=400
        )
    service = NotificationCenterService(NotificationCenterRepository(session))
    updated_count = await service.mark_all_read(user_id=ctx.user_id)
    return {"updatedCount": updated_count, "unreadCount": 0}
