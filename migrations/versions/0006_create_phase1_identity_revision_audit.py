"""create phase 1 tables: user_identities, moment_revisions, audit_events

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-05 13:00:00

补齐 Phase 1 目标表：
- user_identities：外部 OIDC 身份到内部用户的映射
- moment_revisions：每次成功变更后的完整业务快照
- audit_events：只追加的安全与业务审计流
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # user_identities
    op.create_table(
        "user_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issuer", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("issuer", "subject", name="uq_user_identity_issuer_subject"),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])

    # moment_revisions
    op.create_table(
        "moment_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "moment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("moments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("moment_id", "revision", name="uq_moment_revision_id_rev"),
        sa.CheckConstraint("revision >= 1", name="ck_moment_revision_positive"),
    )
    op.create_index("ix_moment_revisions_user_id", "moment_revisions", ["user_id"])
    op.create_index("ix_moment_revisions_moment_id", "moment_revisions", ["moment_id"])

    # audit_events
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_type", sa.String(24), nullable=False),
        sa.Column("actor_id", sa.Text, nullable=True),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("resource_type", sa.Text, nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", sa.Text, nullable=True),
        sa.Column("allowed", sa.Boolean, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_audit_events_user_id", "audit_events", ["user_id"])
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("moment_revisions")
    op.drop_table("user_identities")
