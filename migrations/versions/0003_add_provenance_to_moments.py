"""add provenance jsonb column to moments

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-05 10:00:00

provenance 是 v1 正式字段（moment.v1.json），记录 Moment 来源链：
source(rokid|mobile|web|agent|mcp|import) + 可选 deviceId/clientId/mcpServerId/mcpToolName/externalId。
创建后不可篡改（应用层强制，不在 DB 层做 CHECK）。
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "moments",
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("moments", "provenance")
