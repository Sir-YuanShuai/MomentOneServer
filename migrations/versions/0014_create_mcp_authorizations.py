"""create mcp_authorizations table

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-05 15:30:00

MCP 客户端授权管理（Web 端可查看/调整 scope/撤销）：
- 一次 OAuth 授权（callback 完成）创建/更新一条 authorization
- 撤销后该用户该客户端的 access/refresh token 立即失效（验证时检查）
- scope 调整后下次刷新 token 生效（与 device_bindings 同模式）
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_authorizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("client_id", sa.String(128), nullable=False, index=True),
        sa.Column("client_name", sa.String(200), nullable=True),
        sa.Column("scope", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "client_id", name="uq_mcp_authz_user_client"),
    )


def downgrade() -> None:
    op.drop_table("mcp_authorizations")
