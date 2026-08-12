"""add per-category notification channels

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-12 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, default in (
        ("reminder_channel", "in_app"),
        ("habit_channel", "in_app"),
        ("security_channel", "in_app"),
        ("announcement_channel", "off"),
    ):
        op.add_column(
            "notification_preferences",
            sa.Column(name, sa.String(length=16), nullable=False, server_default=default),
        )
    op.execute(
        """
        UPDATE notification_preferences
        SET reminder_channel = CASE WHEN reminders_enabled AND web_push_enabled THEN 'system' ELSE 'in_app' END,
            habit_channel = CASE WHEN habit_enabled AND web_push_enabled THEN 'system' ELSE 'in_app' END,
            security_channel = CASE WHEN security_enabled AND web_push_enabled THEN 'system' ELSE 'in_app' END,
            announcement_channel = CASE WHEN announcements_enabled AND web_push_enabled THEN 'system' ELSE 'in_app' END
        """
    )


def downgrade() -> None:
    for name in (
        "announcement_channel",
        "security_channel",
        "habit_channel",
        "reminder_channel",
    ):
        op.drop_column("notification_preferences", name)
