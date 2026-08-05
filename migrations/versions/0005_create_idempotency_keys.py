"""create idempotency_keys table

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-05 12:00:00

保存写请求的执行状态和稳定响应，支持 POST /v1/moments 等写操作去重。
相同 (user_id, operation, idempotency_key) 但不同 request_fingerprint
必须返回幂等冲突，不能重放旧结果。
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("request_fingerprint", sa.Text, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="processing"),
        sa.Column("response_status", sa.Integer, nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.UniqueConstraint(
            "user_id", "operation", "idempotency_key", name="uq_idempotency_user_op_key"
        ),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
