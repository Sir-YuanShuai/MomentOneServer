from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth_context
from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.confirmation_repository import (
    SqlConfirmationRepository,
)
from app.infrastructure.database.repositories.idempotency_repository import (
    SqlIdempotencyRepository,
    fingerprint_payload,
)
from app.infrastructure.database.repositories.notification_repository import (
    NotificationPipelineRepository,
    ReminderRepository,
)
from app.infrastructure.database.session import get_db_session
from app.modules.notifications.reminders import ReminderService, serialize_reminder

router = APIRouter(prefix="/v1/reminders", tags=["reminders"])


class ReminderCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    note: str | None = Field(default=None, max_length=2000)
    scene: str = Field(default="general", pattern="^(general|bookkeeping|habit)$")
    remindAt: datetime | None = None
    dueAt: datetime | None = None
    timezone: str = Field(min_length=1, max_length=64)
    sourceMomentId: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def normalize_legacy_due_at(self) -> "ReminderCreateRequest":
        if self.remindAt is None and self.dueAt is not None:
            self.remindAt = self.dueAt
            self.dueAt = None
        if self.remindAt is None:
            raise ValueError("remindAt is required")
        return self


class ReminderUpdateRequest(BaseModel):
    expectedRevision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=160)
    note: str | None = Field(default=None, max_length=2000)
    scene: str | None = Field(default=None, pattern="^(general|bookkeeping|habit)$")
    remindAt: datetime | None = None
    dueAt: datetime | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def normalize_legacy_due_at(self) -> "ReminderUpdateRequest":
        if "remindAt" not in self.model_fields_set and "dueAt" in self.model_fields_set:
            self.remindAt = self.dueAt
            self.dueAt = None
        return self


class ReminderTransitionRequest(BaseModel):
    expectedRevision: int = Field(ge=1)


class ReminderSnoozeRequest(ReminderTransitionRequest):
    remindAt: datetime


class ReminderDeleteConfirmRequest(BaseModel):
    confirmationId: UUID


def _service(session: AsyncSession) -> ReminderService:
    return ReminderService(ReminderRepository(session), NotificationPipelineRepository(session))


@router.get("")
async def list_reminders(
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    items = await ReminderRepository(session).list_by_user(ctx.user_id)
    return {"items": [serialize_reminder(item) for item in items]}


@router.get("/{reminder_id}")
async def get_reminder(
    reminder_id: UUID,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    item = await _service(session).get(user_id=ctx.user_id, reminder_id=reminder_id)
    return serialize_reminder(item)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_reminder(
    body: ReminderCreateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    if not idempotency_key:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED", message="缺少幂等键。", status_code=400
        )
    payload = body.model_dump(mode="json")
    idem = SqlIdempotencyRepository(session)
    record = await idem.acquire(
        user_id=ctx.user_id,
        operation="create_reminder",
        idempotency_key=idempotency_key,
        request_payload=payload,
    )
    if record.request_fingerprint != fingerprint_payload(payload):
        raise ApplicationError(
            code="IDEMPOTENCY_CONFLICT",
            message="Idempotency-Key 已用于不同的请求体。",
            status_code=409,
        )
    if record.state == "completed" and record.response_body:
        return record.response_body
    item = await _service(session).create(
        user_id=ctx.user_id,
        title=body.title,
        body=body.note,
        scene=body.scene,
        due_at=body.remindAt,  # type: ignore[arg-type]  # validator guarantees a value
        deadline_at=body.dueAt,
        timezone=body.timezone,
        source_type="moment" if body.sourceMomentId else "manual",
        source_id=body.sourceMomentId,
        correlation_id=idempotency_key,
    )
    response = serialize_reminder(item)
    await idem.complete(
        record_id=record.id,
        response_status=201,
        response_body=response,
        resource_id=item.id,
    )
    return response


@router.patch("/{reminder_id}")
async def update_reminder(
    reminder_id: UUID,
    body: ReminderUpdateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    if not idempotency_key:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED", message="缺少幂等键。", status_code=400
        )
    item = await _service(session).update(
        user_id=ctx.user_id,
        reminder_id=reminder_id,
        expected_revision=body.expectedRevision,
        title=body.title,
        body=body.note,
        scene=body.scene,
        due_at=body.remindAt,
        deadline_at=body.dueAt,
        timezone=body.timezone,
        correlation_id=idempotency_key,
    )
    return serialize_reminder(item)


async def _transition(
    reminder_id: UUID,
    body: ReminderTransitionRequest,
    target_status: str,
    ctx: AuthContext,
    session: AsyncSession,
    idempotency_key: str | None,
) -> dict:
    if not idempotency_key:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED", message="缺少幂等键。", status_code=400
        )
    item = await _service(session).transition(
        user_id=ctx.user_id,
        reminder_id=reminder_id,
        expected_revision=body.expectedRevision,
        target_status=target_status,
        correlation_id=idempotency_key,
    )
    return serialize_reminder(item)


@router.post("/{reminder_id}/complete")
async def complete_reminder(
    reminder_id: UUID,
    body: ReminderTransitionRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    return await _transition(reminder_id, body, "completed", ctx, session, idempotency_key)


@router.post("/{reminder_id}/cancel")
async def cancel_reminder(
    reminder_id: UUID,
    body: ReminderTransitionRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    return await _transition(reminder_id, body, "cancelled", ctx, session, idempotency_key)


@router.post("/{reminder_id}/snooze")
async def snooze_reminder(
    reminder_id: UUID,
    body: ReminderSnoozeRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    if not idempotency_key:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED", message="缺少幂等键。", status_code=400
        )
    item = await _service(session).snooze(
        user_id=ctx.user_id,
        reminder_id=reminder_id,
        expected_revision=body.expectedRevision,
        remind_at=body.remindAt,
        correlation_id=idempotency_key,
    )
    return serialize_reminder(item)


@router.post("/{reminder_id}/reopen")
async def reopen_reminder(
    reminder_id: UUID,
    body: ReminderTransitionRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    if not idempotency_key:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED", message="缺少幂等键。", status_code=400
        )
    item = await _service(session).reopen(
        user_id=ctx.user_id,
        reminder_id=reminder_id,
        expected_revision=body.expectedRevision,
        correlation_id=idempotency_key,
    )
    return serialize_reminder(item)


@router.post("/{reminder_id}/delete-preview")
async def delete_reminder_preview(
    reminder_id: UUID,
    body: ReminderTransitionRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    item = await _service(session).get(user_id=ctx.user_id, reminder_id=reminder_id)
    ReminderService.assert_revision(item, body.expectedRevision)
    confirmation = await SqlConfirmationRepository(session).create(
        user_id=ctx.user_id,
        target_type="reminder",
        target_id=item.id,
        action="delete",
        expected_revision=item.revision,
        preview={"title": item.title, "remindAt": item.due_at.isoformat()},
        expires_at=datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5),
    )
    return {
        "confirmationId": str(confirmation.id),
        "expiresAt": confirmation.expires_at.isoformat(),
        "revision": item.revision,
        "title": item.title,
    }


@router.post("/delete-confirm", status_code=status.HTTP_204_NO_CONTENT)
async def delete_reminder_confirm(
    body: ReminderDeleteConfirmRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> None:
    if not idempotency_key:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED", message="缺少幂等键。", status_code=400
        )
    confirmations = SqlConfirmationRepository(session)
    confirmation = await confirmations.get(body.confirmationId)
    if (
        confirmation is None
        or confirmation.user_id != ctx.user_id
        or confirmation.target_type != "reminder"
        or confirmation.action != "delete"
    ):
        raise ApplicationError(
            code="CONFIRMATION_REQUIRED", message="请先执行删除预览。", status_code=400
        )
    if confirmation.status != "pending" or datetime.now(UTC) > confirmation.expires_at:
        raise ApplicationError(
            code="CONFIRMATION_EXPIRED", message="确认已失效，请重新预览。", status_code=400
        )
    await _service(session).delete(
        user_id=ctx.user_id,
        reminder_id=confirmation.target_id,
        expected_revision=confirmation.expected_revision,
        correlation_id=idempotency_key,
    )
    await confirmations.mark_used(confirmation_id=confirmation.id, used_at=datetime.now(UTC))
