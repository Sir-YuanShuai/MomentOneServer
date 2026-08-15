"""稳定的 MCP structuredContent 输出契约。

工具函数实际返回 ``CallToolResult``；MCP SDK 会使用这里的返回类型校验其
``structured_content``，并在 ``tools/list`` 暴露对应 ``outputSchema``。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class OutputModel(BaseModel):
    error: dict[str, Any] | None = None
    model_config = ConfigDict(extra="allow")


class MomentItem(OutputModel):
    id: str | None = None
    title: str | None = None
    description: str | None = None
    category: str | None = None
    type: str | None = None
    tags: list[str] = Field(default_factory=list)
    occurredAt: str | None = None


class HabitGoal(OutputModel):
    id: str | None = None
    name: str | None = None
    frequency: str | None = None
    targetPeriod: str | None = None
    targetCount: int | None = None
    revision: int | None = None


class BookkeepingCreateOutput(OutputModel):
    id: str | None = None
    title: str | None = None
    amount: float | None = None
    flow: Literal["expense", "income"] | None = None
    occurredAt: str | None = None
    created: bool | None = None
    replayed: bool | None = None


class BookkeepingListOutput(OutputModel):
    items: list[dict[str, Any]] | None = None
    total: int | None = None
    hasMore: bool | None = None
    nextCursor: str | None = None


class BookkeepingSummaryOutput(OutputModel):
    income: float | None = None
    expense: float | None = None
    balance: float | None = None
    count: int | None = None
    byCategory: list[dict[str, Any]] | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None


class MomentCreateOutput(OutputModel):
    created: bool | None = None
    replayed: bool | None = None
    moment: MomentItem | None = None


class MomentListOutput(OutputModel):
    view: str | None = None
    items: list[MomentItem] | None = None
    total: int | None = None
    hasMore: bool | None = None
    nextCursor: str | None = None


class MomentSearchOutput(MomentListOutput):
    query: str | None = None


class MomentGetOutput(OutputModel):
    moment: MomentItem | None = None
    id: str | None = None
    title: str | None = None


class DailyReviewOutput(OutputModel):
    view: str | None = None
    date: str | None = None
    timezone: str | None = None
    count: int | None = None
    byCategory: dict[str, int] | None = None
    byType: dict[str, int] | None = None
    highlights: list[MomentItem] | None = None
    prompt: str | None = None


class HabitGoalsListOutput(OutputModel):
    goals: list[HabitGoal] | None = None
    total: int | None = None


class HabitGoalCreateOutput(OutputModel):
    created: bool | None = None
    replayed: bool | None = None
    goal: HabitGoal | None = None


class HabitGoalUpdateOutput(OutputModel):
    updated: bool | None = None
    goal: HabitGoal | None = None


class HabitCheckinOutput(OutputModel):
    created: bool | None = None
    replayed: bool | None = None
    goal: HabitGoal | None = None
    checkin: MomentItem | None = None


class HabitProgressOutput(OutputModel):
    view: str | None = None
    timezone: str | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    days: int | None = None
    goals: list[dict[str, Any]] | None = None
    total: int | None = None


class MomentCountOutput(OutputModel):
    count: int | None = None
    byCategory: dict[str, int] | None = None
    byType: dict[str, int] | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None


class AccountEntitlementsOutput(OutputModel):
    planKey: str | None = None
    storage: dict[str, Any] | None = None
    quotas: list[dict[str, Any]] | None = None


class ReminderCreateOutput(OutputModel):
    reminder: dict[str, Any] | None = None
    resolvedTime: dict[str, Any] | None = None


class FeedbackSubmitOutput(OutputModel):
    accepted: bool | None = None
    feedbackId: str | None = None
    status: str | None = None


class AssetUploadIntentOutput(OutputModel):
    assetId: str | None = None
    state: str | None = None
    contentType: str | None = None
    sizeBytes: int | None = None
    upload: dict[str, Any] | None = None
    replayed: bool | None = None


class AssetUploadCompleteOutput(OutputModel):
    assetId: str | None = None
    state: str | None = None
    contentType: str | None = None
    sizeBytes: int | None = None


class AssetImportOutput(AssetUploadCompleteOutput):
    name: str | None = None
    replayed: bool | None = None
    usableBy: list[str] | None = None
