"""create push subscriptions

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-11 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("endpoint_encrypted", sa.Text(), nullable=False),
        sa.Column("p256dh_encrypted", sa.Text(), nullable=False),
        sa.Column("auth_encrypted", sa.Text(), nullable=False),
        sa.Column("content_encoding", sa.String(32), nullable=False, server_default="aes128gcm"),
        sa.Column("expiration_time", sa.DateTime(timezone=True)),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("platform", sa.String(32)),
        sa.Column("device_label", sa.String(120)),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("last_accepted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_push_subscriptions_user_id", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
