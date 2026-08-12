"""add reminder deadline

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-12 11:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reminders", sa.Column("deadline_at", sa.DateTime(timezone=True)))
    op.create_index("ix_reminders_deadline_at", "reminders", ["deadline_at"])


def downgrade() -> None:
    op.drop_index("ix_reminders_deadline_at", table_name="reminders")
    op.drop_column("reminders", "deadline_at")
