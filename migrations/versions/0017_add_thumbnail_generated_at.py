"""add thumbnail_generated_at to assets

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-25

缩略图能力：complete 时对 image 类生成 WebP 缩略图并写入对象存储，
本列标记「已生成」。存量行保持 NULL → 前端降级为图标占位，不影响下载。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assets",
        sa.Column("thumbnail_generated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assets", "thumbnail_generated_at")
