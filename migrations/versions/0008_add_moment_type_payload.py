"""add moment_type and payload to moments

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-08 10:00:00

内置记录类型（docs/domain/MOMENT_RECORD_TYPES.md，D2/D3）：
- moment_type varchar(32) NOT NULL DEFAULT 'general'：记录类型标识，注册表驱动
- payload jsonb NOT NULL DEFAULT '{}'：类型化扩展字段

存量数据零迁移成本（默认值兜底），general = 通用自由记录语义不变。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "moments",
        sa.Column(
            "moment_type",
            sa.String(32),
            nullable=False,
            server_default="general",
        ),
    )
    op.add_column(
        "moments",
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("moments", "payload")
    op.drop_column("moments", "moment_type")
