"""create users and moments tables

Revision ID: 0001
Revises:
Create Date: 2026-08-02 21:46:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("casdoor_sub", sa.String(255), unique=True, nullable=False, index=True),
        sa.Column("casdoor_user_id", sa.String(64), nullable=False, index=True),
        sa.Column("display_name", sa.String(100), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "moments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("title", sa.String(20), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("voice_input", sa.Text, nullable=True),
        sa.Column("ai_summary", sa.Text, nullable=True),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.String), nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(50), nullable=False),
        sa.Column("location_name", sa.String(200), nullable=True),
        sa.Column("location_latitude", sa.Float, nullable=True),
        sa.Column("location_longitude", sa.Float, nullable=True),
        sa.Column("location_source", sa.String(20), nullable=True),
        sa.Column("emotion_label", sa.String(50), nullable=True),
        sa.Column("emotion_score", sa.Float, nullable=True),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("moments")
    op.drop_table("users")
