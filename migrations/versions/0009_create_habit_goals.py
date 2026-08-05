"""create habit_goals table

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-08 11:00:00

习惯养成（docs/domain/MOMENT_RECORD_TYPES.md §3.3 演进）：
- habit_goals：用户设定的习惯目标（游泳 / 跑步 / 喝水…），是打卡记录的挂靠对象
- 打卡记录仍为 type=habit 的 Moment，payload.goalId 关联本表（逻辑引用，不做物理 FK，
  由应用层校验归属；习惯删除采用软删除，历史打卡记录保留）
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "habit_goals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("name", sa.String(30), nullable=False),
        sa.Column("unit", sa.String(20), nullable=True),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("habit_goals")
