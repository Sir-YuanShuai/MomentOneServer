"""习惯目标领域对象（习惯养成的挂靠实体）。

打卡记录（type=habit 的 Moment）通过 payload.goalId 逻辑引用 HabitGoal。
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class HabitGoal:
    id: UUID
    user_id: UUID
    name: str
    revision: int
    created_at: datetime
    updated_at: datetime
    unit: str | None = None
    frequency: str | None = None  # daily / weekly
    times_per_week: int | None = None
    target_period: str = "daily"
    target_count: int = 1
    color: str | None = None
    deleted_at: datetime | None = None
