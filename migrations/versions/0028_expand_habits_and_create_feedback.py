"""expand habit targets and create user feedback

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-13 22:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "habit_goals",
        sa.Column("target_period", sa.String(16), nullable=False, server_default="daily"),
    )
    op.add_column(
        "habit_goals",
        sa.Column("target_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.execute(
        "UPDATE habit_goals SET target_period = 'weekly', "
        "target_count = COALESCE(times_per_week, 1) WHERE frequency = 'weekly'"
    )
    op.create_check_constraint(
        "ck_habit_goal_target_period",
        "habit_goals",
        "target_period IN ('daily', 'weekly', 'monthly')",
    )
    op.create_check_constraint("ck_habit_goal_target_count", "habit_goals", "target_count > 0")
    op.create_table(
        "user_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("summary", sa.String(160), nullable=False),
        sa.Column("details", sa.Text()),
        sa.Column("context", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("source", sa.String(24), nullable=False, server_default="mcp"),
        sa.Column("status", sa.String(24), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_user_feedback_user_created", "user_feedback", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_user_feedback_user_created", table_name="user_feedback")
    op.drop_table("user_feedback")
    op.drop_constraint("ck_habit_goal_target_count", "habit_goals", type_="check")
    op.drop_constraint("ck_habit_goal_target_period", "habit_goals", type_="check")
    op.drop_column("habit_goals", "target_count")
    op.drop_column("habit_goals", "target_period")
