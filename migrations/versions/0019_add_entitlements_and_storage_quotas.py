"""add entitlement and storage quota foundation

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-09 14:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GIB = 1024 * 1024 * 1024


def upgrade() -> None:
    op.create_table(
        "plan_definitions",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "entitlements",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "quotas",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("version >= 1", name="ck_plan_definitions_version_positive"),
        sa.CheckConstraint("status IN ('active', 'retired')", name="ck_plan_definitions_status"),
    )
    op.create_table(
        "user_entitlements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entitlement_key", sa.String(128), nullable=False),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "starts_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "user_id",
            "entitlement_key",
            "source_type",
            "source_ref",
            name="uq_user_entitlements_source",
        ),
        sa.CheckConstraint(
            "source_type IN ('default', 'admin', 'order', 'subscription', 'promotion')",
            name="ck_user_entitlements_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'revoked')", name="ck_user_entitlements_status"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_user_entitlements_revision_positive"),
    )
    op.create_index("ix_user_entitlements_user_id", "user_entitlements", ["user_id"])
    op.create_index(
        "ix_user_entitlements_entitlement_key", "user_entitlements", ["entitlement_key"]
    )

    op.create_table(
        "user_storage_accounts",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("effective_quota_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("over_quota", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reconciled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("used_bytes >= 0", name="ck_user_storage_used_nonneg"),
        sa.CheckConstraint("reserved_bytes >= 0", name="ck_user_storage_reserved_nonneg"),
        sa.CheckConstraint("effective_quota_bytes >= 0", name="ck_user_storage_quota_nonneg"),
        sa.CheckConstraint("revision >= 1", name="ck_user_storage_revision_positive"),
    )
    op.create_index("ix_user_storage_accounts_over_quota", "user_storage_accounts", ["over_quota"])

    op.create_table(
        "storage_quota_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "starts_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "user_id", "source_type", "source_ref", name="uq_storage_grants_source"
        ),
        sa.CheckConstraint(
            "source_type IN ('default', 'admin', 'order', 'subscription', 'promotion')",
            name="ck_storage_grants_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'revoked')", name="ck_storage_grants_status"
        ),
        sa.CheckConstraint("quota_bytes >= 0", name="ck_storage_grants_quota_nonneg"),
        sa.CheckConstraint("revision >= 1", name="ck_storage_grants_revision_positive"),
    )
    op.create_index("ix_storage_quota_grants_user_id", "storage_quota_grants", ["user_id"])

    plans = sa.table(
        "plan_definitions",
        sa.column("key", sa.String),
        sa.column("version", sa.Integer),
        sa.column("name", sa.Text),
        sa.column("status", sa.String),
        sa.column("entitlements", postgresql.JSONB),
        sa.column("quotas", postgresql.JSONB),
    )
    op.bulk_insert(
        plans,
        [
            {
                "key": "free",
                "version": 1,
                "name": "Free",
                "status": "active",
                "entitlements": {"moment.core": True, "media.upload": True},
                "quotas": {"storage_bytes": GIB, "max_upload_bytes": 20 * 1024 * 1024},
            },
            {
                "key": "plus",
                "version": 1,
                "name": "Plus",
                "status": "active",
                "entitlements": {
                    "moment.core": True,
                    "media.upload": True,
                    "history.extended": True,
                },
                "quotas": {"storage_bytes": 10 * GIB, "max_upload_bytes": 100 * 1024 * 1024},
            },
            {
                "key": "pro",
                "version": 1,
                "name": "Pro",
                "status": "active",
                "entitlements": {
                    "moment.core": True,
                    "media.upload": True,
                    "history.extended": True,
                    "automation.advanced": True,
                },
                "quotas": {"storage_bytes": 50 * GIB, "max_upload_bytes": 500 * 1024 * 1024},
            },
        ],
    )

    op.execute(
        sa.text(
            """
            INSERT INTO user_entitlements (
                id, user_id, entitlement_key, source_type, source_ref,
                status, starts_at, metadata, revision
            )
            SELECT gen_random_uuid(), id, 'plan:free', 'default', 'plan:free', 'active', now(),
                   '{"planKey":"free"}'::jsonb, 1
            FROM users
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO user_entitlements (
                id, user_id, entitlement_key, source_type, source_ref,
                status, starts_at, metadata, revision
            )
            SELECT gen_random_uuid(), id, capability, 'default', 'plan:free',
                   'active', now(), '{"planKey":"free"}'::jsonb, 1
            FROM users
            CROSS JOIN (VALUES ('moment.core'), ('media.upload')) AS capabilities(capability)
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO storage_quota_grants
                (id, user_id, source_type, source_ref, quota_bytes, status, starts_at, revision)
            SELECT gen_random_uuid(), id, 'default', 'plan:free', {GIB}, 'active', now(), 1
            FROM users
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO user_storage_accounts (
                user_id, used_bytes, reserved_bytes, effective_quota_bytes,
                over_quota, revision, reconciled_at
            )
            SELECT u.id,
                   COALESCE(SUM(a.size_bytes) FILTER (WHERE a.state = 'ready'), 0),
                   COALESCE(SUM(a.size_bytes) FILTER (WHERE a.state = 'uploading'), 0),
                   {GIB},
                   COALESCE(
                       SUM(a.size_bytes) FILTER (WHERE a.state IN ('ready', 'uploading')), 0
                   ) > {GIB},
                   1,
                   now()
            FROM users u
            LEFT JOIN assets a ON a.user_id = u.id AND a.deleted_at IS NULL
            GROUP BY u.id
            """
        )
    )


def downgrade() -> None:
    op.drop_table("storage_quota_grants")
    op.drop_table("user_storage_accounts")
    op.drop_table("user_entitlements")
    op.drop_table("plan_definitions")
