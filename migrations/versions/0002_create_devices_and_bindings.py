"""create devices, device_bindings, binding_codes tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-04 13:00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 设备注册表：device_id 由眼镜端首次生成 UUID v4，Server upsert
    op.create_table(
        "devices",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("device_type", sa.String(48), nullable=True),
        sa.Column("device_name", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # 绑定关系（长期）
    op.create_table(
        "device_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("scope", postgresql.ARRAY(sa.String), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("refresh_token_hash", sa.String(128), nullable=True),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
    )

    # 一副眼镜只能绑定一个活跃用户（部分唯一索引）
    op.create_index(
        "uq_device_bindings_device_active",
        "device_bindings",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # 绑定会话码（短期，5 分钟过期，一次性）
    op.create_table(
        "binding_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("scope", postgresql.ARRAY(sa.String), nullable=False, server_default="{}"),
        sa.Column("device_name", sa.String(120), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("binding_codes")
    op.drop_table("device_bindings")
    op.drop_table("devices")
