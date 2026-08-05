"""MCP OAuth 存储模型。

- mcp_oauth_clients：DCR（RFC 7591）注册的客户端
- mcp_oauth_codes：授权码 / Casdoor 事务状态（PKCE challenge、回调 state）
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class McpOAuthClient(Base):
    """DCR 注册的 MCP 客户端（RFC 7591）。"""

    __tablename__ = "mcp_oauth_clients"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    client_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    client_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    redirect_uris: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    scope: Mapped[str] = mapped_column(String(512), nullable=False, default="moments.read")
    grant_types: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    token_endpoint_auth_method: Mapped[str] = mapped_column(
        String(32), nullable=False, default="none"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class McpOAuthCode(Base):
    """授权码 / Casdoor 事务记录。

    - kind='casdoor_txn'：authorize 阶段创建的 Casdoor 跳转事务（code=casdoor_state）
    - kind='auth_code'：callback 完成后签发的授权码（code 由客户端换取 token）
    """

    __tablename__ = "mcp_oauth_codes"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # casdoor_txn | auth_code
    client_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    redirect_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(String(512), nullable=True)
    state: Mapped[str | None] = mapped_column(Text, nullable=True)  # 客户端 state（原样回传）
    code_challenge: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 客户端 PKCE
    casdoor_code_verifier: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resource: Mapped[str | None] = mapped_column(String(512), nullable=True)  # RFC 8707 资源指示符
    user_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class McpAuthorization(Base):
    """MCP 客户端授权关系（Web 端可管理：调整 scope / 撤销）。

    一次 OAuth 授权（callback 完成）创建/更新一条记录；撤销后该用户
    该客户端的 token 立即失效（验证时检查 status）。
    """

    __tablename__ = "mcp_authorizations"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    client_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    client_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scope: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
