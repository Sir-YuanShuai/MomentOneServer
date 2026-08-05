"""create mcp_oauth tables

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-09 12:00:00

MCP OAuth 授权代理（docs/roadmap/MCP_APPS_PLAN.md §3）：
- mcp_oauth_clients：DCR（RFC 7591）注册的 MCP 客户端
- mcp_oauth_codes：授权码 + Casdoor 事务状态（PKCE challenge / 回调 state）

说明：MCP Server 作为 OAuth 授权服务器代理（authorize/token/register 在我们这边），
token 由 Server 自签 RS256（与眼镜端 QR Binding 同一套 JwtIssuer），
因此 MCP 客户端注册表和授权码需要持久化（客户端注册不能因重启丢失）。
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_oauth_clients",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", sa.String(128), nullable=False, unique=True),
        sa.Column("client_name", sa.String(200), nullable=True),
        sa.Column("redirect_uris", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("scope", sa.String(512), nullable=False, server_default="moments.read"),
        sa.Column("grant_types", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "token_endpoint_auth_method",
            sa.String(32),
            nullable=False,
            server_default="none",
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "mcp_oauth_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(128), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("client_id", sa.String(128), nullable=False, index=True),
        sa.Column("redirect_uri", sa.Text, nullable=True),
        sa.Column("scope", sa.String(512), nullable=True),
        sa.Column("state", sa.Text, nullable=True),
        sa.Column("code_challenge", sa.String(128), nullable=True),
        sa.Column("casdoor_code_verifier", sa.String(128), nullable=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("mcp_oauth_codes")
    op.drop_table("mcp_oauth_clients")
