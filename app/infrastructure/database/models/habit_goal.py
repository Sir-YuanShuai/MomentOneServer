from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class HabitGoal(Base):
    """习惯目标：用户设定的习惯养成对象（游泳 / 跑步 / 喝水…）。

    打卡记录（type=habit 的 Moment）通过 payload.goalId 逻辑引用本表，
    由应用层校验归属；删除走软删除（deleted_at），历史打卡记录保留。
    """

    __tablename__ = "habit_goals"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String(30))
    unit: Mapped[str | None] = mapped_column(String(20))
    frequency: Mapped[str | None] = mapped_column(String(16))  # daily / weekly
    times_per_week: Mapped[int | None] = mapped_column(Integer)
    color: Mapped[str | None] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
