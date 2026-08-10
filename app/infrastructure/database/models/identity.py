from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class UserIdentity(Base):
    """外部身份、邮箱和手机号到内部 User 的唯一映射。"""

    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_user_identity_issuer_subject"),
        CheckConstraint(
            "identity_type IN ('oidc', 'email', 'phone', 'provider')",
            name="ck_user_identity_type",
        ),
        CheckConstraint(
            "status IN ('active', 'unlinked')",
            name="ck_user_identity_status",
        ),
        CheckConstraint("revision >= 1", name="ck_user_identity_revision_positive"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    identity_type: Mapped[str] = mapped_column(
        String(24), nullable=False, default="oidc", server_default="oidc"
    )
    provider: Mapped[str] = mapped_column(
        String(64), nullable=False, default="casdoor", server_default="casdoor"
    )
    identifier: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active", index=True
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AccountLinkSession(Base):
    """当前用户把另一个已验证 Casdoor/OIDC 身份关联到内部 User 的短期事务。"""

    __tablename__ = "account_link_sessions"
    __table_args__ = (
        UniqueConstraint("state", name="uq_account_link_sessions_state"),
        CheckConstraint(
            "status IN ('pending', 'linked', 'already_linked', 'conflict', 'expired', 'failed')",
            name="ck_account_link_session_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64))
    return_uri: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending", index=True
    )
    linked_identity_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    conflict_user_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), index=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    metadata_: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContactVerificationChallenge(Base):
    """邮箱/手机号变更的验证事务；验证码由 Casdoor 发送与校验。"""

    __tablename__ = "contact_verification_challenges"
    __table_args__ = (
        CheckConstraint("kind IN ('email', 'phone')", name="ck_contact_challenge_kind"),
        CheckConstraint(
            "status IN ('pending', 'verified', 'canceled', 'expired', 'failed')",
            name="ck_contact_challenge_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_contact_challenge_attempts_nonneg"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    destination: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(8))
    previous_value: Mapped[str | None] = mapped_column(Text)
    previous_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
