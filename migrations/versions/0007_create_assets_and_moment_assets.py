"""create assets and moment_assets tables

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-06 10:00:00

Phase 2 媒体表：
- assets：媒体业务元数据（字节存放在 MinIO/S3）
- moment_assets：Moment 与 Asset 的有序关联

同时补齐 moments 推荐约束 UNIQUE (user_id, id)，使 moment_assets
可以使用复合外键 (user_id, moment_id) REFERENCES moments(user_id, id)
实现数据库级防跨用户关联。
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 补齐 moments 复合唯一约束，支持 moment_assets 复合外键
    op.create_unique_constraint(
        "uq_moments_user_id_id",
        "moments",
        ["user_id", "id"],
    )

    # assets：媒体业务元数据
    op.create_table(
        "assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="uploading"),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False, unique=True),
        sa.Column("content_type", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=True),
        sa.Column("checksum_sha256", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("state IN ('uploading', 'ready', 'detached', 'failed', 'purged')", name="ck_assets_state"),
        sa.CheckConstraint("kind IN ('image', 'audio', 'video', 'document')", name="ck_assets_kind"),
        sa.CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_assets_size_nonneg"),
        sa.UniqueConstraint("user_id", "id", name="uq_assets_user_id_id"),
    )
    op.create_index(
        "ix_assets_user_state",
        "assets",
        ["user_id", "state"],
    )

    # moment_assets：Moment 与 Asset 的有序关联
    op.create_table(
        "moment_assets",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("moment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.SmallInteger, nullable=False),
        sa.Column("role", sa.String(24), nullable=False, server_default="original"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id", "moment_id"], ["moments.user_id", "moments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id", "asset_id"], ["assets.user_id", "assets.id"], ondelete="CASCADE"),
        sa.CheckConstraint("position >= 0", name="ck_moment_assets_position_nonneg"),
        sa.CheckConstraint("role IN ('original', 'cover', 'voice_note', 'attachment')", name="ck_moment_assets_role"),
        sa.PrimaryKeyConstraint("moment_id", "asset_id", name="pk_moment_assets"),
        sa.UniqueConstraint("moment_id", "position", name="uq_moment_assets_moment_position"),
    )
    op.create_index(
        "ix_moment_assets_user_moment",
        "moment_assets",
        ["user_id", "moment_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_moment_assets_user_moment", table_name="moment_assets")
    op.drop_table("moment_assets")
    op.drop_index("ix_assets_user_state", table_name="assets")
    op.drop_table("assets")
    op.drop_constraint("uq_moments_user_id_id", "moments", type_="unique")
