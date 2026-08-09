"""add admin operations fields

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-09 00:00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("status", sa.String(16), nullable=False, server_default="active")
    )
    op.add_column(
        "users", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("users", sa.Column("last_active_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("disabled_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("disable_reason", sa.String(240)))
    op.create_index("ix_users_status", "users", ["status"])

    op.add_column(
        "device_bindings", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column(
        "mcp_authorizations", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )


def downgrade() -> None:
    op.drop_column("mcp_authorizations", "revision")
    op.drop_column("device_bindings", "revision")
    op.drop_index("ix_users_status", table_name="users")
    op.drop_column("users", "disable_reason")
    op.drop_column("users", "disabled_at")
    op.drop_column("users", "last_active_at")
    op.drop_column("users", "revision")
    op.drop_column("users", "status")
