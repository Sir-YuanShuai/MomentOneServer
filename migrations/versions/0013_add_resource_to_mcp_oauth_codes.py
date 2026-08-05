"""add resource column to mcp_oauth_codes

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-05 14:30:00

RFC 8707 资源指示符：ChatGPT 在 authorize 请求中携带 resource，
token 签发的 aud 需与 resource 一致（严格 Host 会校验）。
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mcp_oauth_codes", sa.Column("resource", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_oauth_codes", "resource")
