"""add quota usage and api metrics

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-09 16:00:00
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_url", sa.Text()))

    op.create_table(
        "quota_accounts",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("quota_key", sa.String(128), nullable=False),
        sa.Column("limit_value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("used_value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("user_id", "quota_key", "period_start", name="pk_quota_accounts"),
        sa.CheckConstraint("limit_value >= 0", name="ck_quota_accounts_limit_nonneg"),
        sa.CheckConstraint("used_value >= 0", name="ck_quota_accounts_used_nonneg"),
        sa.CheckConstraint("reserved_value >= 0", name="ck_quota_accounts_reserved_nonneg"),
        sa.CheckConstraint("revision >= 1", name="ck_quota_accounts_revision_positive"),
    )
    op.create_index("ix_quota_accounts_quota_key", "quota_accounts", ["quota_key"])

    op.create_table(
        "quota_usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("quota_key", sa.String(128), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("operation_key", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.String(24), nullable=False),
        sa.Column("device_id", sa.Text()),
        sa.Column("client_id", sa.Text()),
        sa.Column("tool_name", sa.Text()),
        sa.Column("idempotency_key", sa.Text()),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.UniqueConstraint(
            "user_id", "quota_key", "operation_key", name="uq_quota_usage_operation"
        ),
        sa.CheckConstraint("amount >= 0", name="ck_quota_usage_amount_nonneg"),
    )
    op.create_index("ix_quota_usage_events_user_id", "quota_usage_events", ["user_id"])
    op.create_index("ix_quota_usage_events_quota_key", "quota_usage_events", ["quota_key"])
    op.create_index("ix_quota_usage_events_tool_name", "quota_usage_events", ["tool_name"])
    op.create_index("ix_quota_usage_events_occurred_at", "quota_usage_events", ["occurred_at"])

    op.create_table(
        "api_usage_buckets",
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("route", sa.String(240), nullable=False),
        sa.Column("method", sa.String(12), nullable=False),
        sa.Column("request_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("latency_ms_total", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("bucket_start", "route", "method", name="pk_api_usage_buckets"),
        sa.CheckConstraint("request_count >= 0", name="ck_api_usage_requests_nonneg"),
        sa.CheckConstraint("error_count >= 0", name="ck_api_usage_errors_nonneg"),
        sa.CheckConstraint("latency_ms_total >= 0", name="ck_api_usage_latency_nonneg"),
    )

    plan_quotas = {
        "free": {
            "mcp.tool_calls.month": 1000,
            "mcp.write_calls.month": 100,
            "mcp.agent_plan.day": 30,
            "device.active": 1,
            "mcp.clients.active": 1,
            "api.requests.month": 5000,
            "ai.tokens.month": 0,
        },
        "plus": {
            "mcp.tool_calls.month": 10000,
            "mcp.write_calls.month": 2000,
            "mcp.agent_plan.day": 300,
            "device.active": 3,
            "mcp.clients.active": 5,
            "api.requests.month": 50000,
            "ai.tokens.month": 1000000,
        },
        "pro": {
            "mcp.tool_calls.month": 100000,
            "mcp.write_calls.month": 20000,
            "mcp.agent_plan.day": 3000,
            "device.active": 10,
            "mcp.clients.active": 20,
            "api.requests.month": 500000,
            "ai.tokens.month": 10000000,
        },
    }
    for key, quotas in plan_quotas.items():
        op.execute(
            sa.text(
                """
                UPDATE plan_definitions
                SET quotas = quotas || CAST(:quotas AS jsonb),
                    version = version + 1,
                    updated_at = now()
                WHERE key = :key
                """
            ).bindparams(key=key, quotas=json.dumps(quotas))
        )


def downgrade() -> None:
    op.drop_table("api_usage_buckets")
    op.drop_table("quota_usage_events")
    op.drop_table("quota_accounts")
    op.drop_column("users", "avatar_url")
