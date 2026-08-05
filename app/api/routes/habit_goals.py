"""习惯目标（habit goals）管理接口。

- POST   /v1/habit-goals                     创建习惯目标
- GET    /v1/habit-goals                     列表
- GET    /v1/habit-goals/{id}                详情
- PATCH  /v1/habit-goals/{id}                修改（乐观锁 expectedRevision）
- POST   /v1/habit-goals/{id}/delete-preview  删除预览（两阶段）
- POST   /v1/habit-goals/delete-confirm       删除确认

删除走两阶段 Preview + Confirm（根 AGENTS.md 业务规则），复用 pending_confirmations。
打卡记录（type=habit 的 Moment）通过 payload.goalId 逻辑引用本表。
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_authenticated_user_id
from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.confirmation_repository import (
    SqlConfirmationRepository,
)
from app.infrastructure.database.repositories.habit_goal_repository import (
    SqlHabitGoalRepository,
)
from app.infrastructure.database.session import get_db_session
from app.modules.habit_goals.domain import HabitGoal

router = APIRouter(prefix="/v1/habit-goals", tags=["habit-goals"])


async def _get_user_id(
    user_id: UUID = Depends(get_authenticated_user_id),
) -> UUID:
    return user_id


def _to_dict(goal: HabitGoal) -> dict:
    return {
        "id": str(goal.id),
        "userId": str(goal.user_id),
        "name": goal.name,
        "unit": goal.unit,
        "frequency": goal.frequency,
        "timesPerWeek": goal.times_per_week,
        "color": goal.color,
        "revision": goal.revision,
        "createdAt": goal.created_at.isoformat(),
        "updatedAt": goal.updated_at.isoformat(),
        "deletedAt": goal.deleted_at.isoformat() if goal.deleted_at else None,
    }


class CreateHabitGoalRequest(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    unit: str | None = Field(default=None, max_length=20)
    frequency: str | None = Field(default=None, max_length=16)
    timesPerWeek: int | None = Field(default=None, ge=1, le=30)
    color: str | None = Field(default=None, max_length=16)


class UpdateHabitGoalRequest(BaseModel):
    expectedRevision: int
    name: str | None = Field(default=None, min_length=1, max_length=30)
    unit: str | None = Field(default=None, max_length=20)
    frequency: str | None = Field(default=None, max_length=16)
    timesPerWeek: int | None = Field(default=None, ge=1, le=30)
    color: str | None = Field(default=None, max_length=16)


class DeletePreviewRequest(BaseModel):
    expectedRevision: int


class DeleteConfirmRequest(BaseModel):
    confirmationId: str


class HabitGoalListResponse(BaseModel):
    items: list[dict]


@router.get("", response_model=HabitGoalListResponse)
async def list_habit_goals(
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> HabitGoalListResponse:
    repo = SqlHabitGoalRepository(session)
    goals = await repo.list_by_user(user_id)
    return HabitGoalListResponse(items=[_to_dict(g) for g in goals])


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_habit_goal(
    body: CreateHabitGoalRequest,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    now = datetime.now(UTC)
    goal = HabitGoal(
        id=uuid4(),
        user_id=user_id,
        name=body.name.strip(),
        unit=body.unit.strip() if body.unit else None,
        frequency=body.frequency,
        times_per_week=body.timesPerWeek,
        color=body.color,
        revision=1,
        created_at=now,
        updated_at=now,
    )
    if not goal.name:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="习惯名称不能为空。",
            status_code=400,
        )
    repo = SqlHabitGoalRepository(session)
    created = await repo.create(goal)
    return _to_dict(created)


@router.get("/{goal_id}", response_model=dict)
async def get_habit_goal(
    goal_id: str,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    repo = SqlHabitGoalRepository(session)
    goal = await repo.get_by_id(UUID(goal_id), user_id)
    if goal is None:
        raise ApplicationError(
            code="HABIT_GOAL_NOT_FOUND",
            message="未找到该习惯目标。",
            status_code=404,
        )
    return _to_dict(goal)


@router.patch("/{goal_id}", response_model=dict)
async def update_habit_goal(
    goal_id: str,
    body: UpdateHabitGoalRequest,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    repo = SqlHabitGoalRepository(session)
    existing = await repo.get_by_id(UUID(goal_id), user_id)
    if existing is None:
        raise ApplicationError(
            code="HABIT_GOAL_NOT_FOUND",
            message="未找到该习惯目标。",
            status_code=404,
        )
    if existing.revision != body.expectedRevision:
        raise ApplicationError(
            code="REVISION_CONFLICT",
            message="习惯目标已被其他操作修改，请刷新后重试。",
            status_code=409,
            details={
                "expectedRevision": body.expectedRevision,
                "actualRevision": existing.revision,
            },
        )
    fields = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.unit is not None:
        fields["unit"] = body.unit.strip() if body.unit.strip() else None
    if body.frequency is not None:
        fields["frequency"] = body.frequency
    if body.timesPerWeek is not None:
        fields["times_per_week"] = body.timesPerWeek
    if body.color is not None:
        fields["color"] = body.color
    updated = await repo.update(UUID(goal_id), user_id, **fields)
    if updated is None:
        raise ApplicationError(
            code="HABIT_GOAL_NOT_FOUND",
            message="未找到该习惯目标。",
            status_code=404,
        )
    return _to_dict(updated)


@router.post("/{goal_id}/delete-preview", response_model=dict)
async def delete_preview(
    goal_id: str,
    body: DeletePreviewRequest,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    repo = SqlHabitGoalRepository(session)
    goal = await repo.get_by_id(UUID(goal_id), user_id)
    if goal is None:
        raise ApplicationError(
            code="HABIT_GOAL_NOT_FOUND",
            message="未找到该习惯目标。",
            status_code=404,
        )
    if goal.revision != body.expectedRevision:
        raise ApplicationError(
            code="REVISION_CONFLICT",
            message="习惯目标已被其他操作修改，请刷新后重试。",
            status_code=409,
            details={
                "expectedRevision": body.expectedRevision,
                "actualRevision": goal.revision,
            },
        )
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)
    preview = {"name": goal.name}
    confirmation_repo = SqlConfirmationRepository(session)
    confirmation = await confirmation_repo.create(
        user_id=user_id,
        target_type="habit_goal",
        target_id=goal.id,
        action="delete",
        expected_revision=goal.revision,
        preview=preview,
        expires_at=expires_at,
    )
    return {
        "confirmationId": str(confirmation.id),
        "expiresAt": confirmation.expires_at.isoformat(),
        "revision": goal.revision,
    }


@router.post("/delete-confirm", status_code=status.HTTP_204_NO_CONTENT)
async def delete_confirm(
    body: DeleteConfirmRequest,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    confirmation_repo = SqlConfirmationRepository(session)
    confirmation = await confirmation_repo.get(UUID(body.confirmationId))
    if confirmation is None or confirmation.user_id != user_id:
        raise ApplicationError(
            code="CONFIRMATION_REQUIRED",
            message="请先执行删除预览。",
            status_code=400,
        )
    if confirmation.status == "used":
        raise ApplicationError(
            code="CONFIRMATION_USED",
            message="该确认已使用，请重新发起删除。",
            status_code=400,
        )
    if datetime.now(UTC) > confirmation.expires_at:
        raise ApplicationError(
            code="CONFIRMATION_EXPIRED",
            message="确认已过期，请重新发起删除。",
            status_code=400,
        )
    await confirmation_repo.mark_used(confirmation_id=confirmation.id, used_at=datetime.now(UTC))
    repo = SqlHabitGoalRepository(session)
    deleted = await repo.soft_delete(confirmation.target_id, user_id)
    if deleted is None:
        raise ApplicationError(
            code="HABIT_GOAL_NOT_FOUND",
            message="未找到该习惯目标。",
            status_code=404,
        )
