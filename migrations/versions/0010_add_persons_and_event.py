"""add persons and event to moments

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-08 12:00:00

通用描述维度（ADR-0019）：所有 Moment 可选的「人物 / 事件」字段，
配合既有 occurred_at（时间）、location（地点），构成 时间-地点-人物-事件 描述体系。
均可留空，非必要字段。
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "moments",
        sa.Column(
            "persons",
            postgresql.ARRAY(sa.String),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "moments",
        sa.Column("event_name", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("moments", "event_name")
    op.drop_column("moments", "persons")
