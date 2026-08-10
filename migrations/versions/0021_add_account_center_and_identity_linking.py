"""add account center and identity linking

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-09 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.String(32)))
    op.add_column(
        "users", sa.Column("email_verified", sa.Boolean(), nullable=False, server_default="false")
    )
    op.add_column(
        "users", sa.Column("phone_verified", sa.Boolean(), nullable=False, server_default="false")
    )
    op.add_column(
        "users", sa.Column("locale", sa.String(16), nullable=False, server_default="zh-CN")
    )
    op.add_column("users", sa.Column("timezone", sa.String(64)))
    op.add_column(
        "users",
        sa.Column(
            "profile_sync_status", sa.String(16), nullable=False, server_default="local_only"
        ),
    )
    op.add_column("users", sa.Column("profile_sync_error", sa.Text()))
    op.add_column("users", sa.Column("profile_synced_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_users_profile_sync_status",
        "users",
        "profile_sync_status IN ('local_only', 'pending', 'synced', 'failed')",
    )

    op.add_column(
        "user_identities",
        sa.Column("identity_type", sa.String(24), nullable=False, server_default="oidc"),
    )
    op.add_column(
        "user_identities",
        sa.Column("provider", sa.String(64), nullable=False, server_default="casdoor"),
    )
    op.add_column("user_identities", sa.Column("identifier", sa.Text()))
    op.add_column("user_identities", sa.Column("display_name", sa.Text()))
    op.add_column(
        "user_identities",
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    )
    op.add_column(
        "user_identities",
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("user_identities", sa.Column("verified_at", sa.DateTime(timezone=True)))
    op.add_column(
        "user_identities",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "user_identities", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column(
        "user_identities",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_user_identities_status", "user_identities", ["status"])
    op.create_check_constraint(
        "ck_user_identity_type",
        "user_identities",
        "identity_type IN ('oidc', 'email', 'phone', 'provider')",
    )
    op.create_check_constraint(
        "ck_user_identity_status",
        "user_identities",
        "status IN ('active', 'unlinked')",
    )
    op.create_check_constraint(
        "ck_user_identity_revision_positive", "user_identities", "revision >= 1"
    )

    op.create_table(
        "account_link_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(64)),
        sa.Column("return_uri", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("linked_identity_id", postgresql.UUID(as_uuid=True)),
        sa.Column("conflict_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("state", name="uq_account_link_sessions_state"),
        sa.CheckConstraint(
            "status IN ('pending', 'linked', 'already_linked', 'conflict', 'expired', 'failed')",
            name="ck_account_link_session_status",
        ),
    )
    op.create_index("ix_account_link_sessions_user_id", "account_link_sessions", ["user_id"])
    op.create_index("ix_account_link_sessions_status", "account_link_sessions", ["status"])
    op.create_index(
        "ix_account_link_sessions_conflict_user_id", "account_link_sessions", ["conflict_user_id"]
    )
    op.create_index("ix_account_link_sessions_expires_at", "account_link_sessions", ["expires_at"])

    op.create_table(
        "contact_verification_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("destination", sa.Text(), nullable=False),
        sa.Column("country_code", sa.String(8)),
        sa.Column("previous_value", sa.Text()),
        sa.Column("previous_verified", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('email', 'phone')", name="ck_contact_challenge_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'verified', 'canceled', 'expired', 'failed')",
            name="ck_contact_challenge_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_contact_challenge_attempts_nonneg"),
    )
    op.create_index(
        "ix_contact_verification_challenges_user_id",
        "contact_verification_challenges",
        ["user_id"],
    )
    op.create_index(
        "ix_contact_verification_challenges_status",
        "contact_verification_challenges",
        ["status"],
    )
    op.create_index(
        "ix_contact_verification_challenges_expires_at",
        "contact_verification_challenges",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_table("contact_verification_challenges")
    op.drop_table("account_link_sessions")
    op.drop_constraint("ck_user_identity_revision_positive", "user_identities", type_="check")
    op.drop_constraint("ck_user_identity_status", "user_identities", type_="check")
    op.drop_constraint("ck_user_identity_type", "user_identities", type_="check")
    op.drop_index("ix_user_identities_status", table_name="user_identities")
    for column in (
        "updated_at",
        "revision",
        "metadata",
        "verified_at",
        "is_primary",
        "status",
        "display_name",
        "identifier",
        "provider",
        "identity_type",
    ):
        op.drop_column("user_identities", column)
    op.drop_constraint("ck_users_profile_sync_status", "users", type_="check")
    for column in (
        "profile_synced_at",
        "profile_sync_error",
        "profile_sync_status",
        "timezone",
        "locale",
        "phone_verified",
        "email_verified",
        "phone",
    ):
        op.drop_column("users", column)
