"""add frequency and color to habit_goals

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-08 14:00:00

习惯定义补充（参考 Loop / Streaks / 小日常 等习惯打卡 App）：
- frequency：打卡频率（daily=每天 / weekly=每周 N 次），可选，仅存储与展示，
  不驱动校验（MVP）
- times_per_week：frequency=weekly 时的每周目标次数
- color：习惯标识色（日历/卡片区分），可选
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "habit_goals",
        sa.Column("frequency", sa.String(16), nullable=True),
    )
    op.add_column(
        "habit_goals",
        sa.Column("times_per_week", sa.Integer, nullable=True),
    )
    op.add_column(
        "habit_goals",
        sa.Column("color", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("habit_goals", "color")
    op.drop_column("habit_goals", "times_per_week")
    op.drop_column("habit_goals", "frequency")
