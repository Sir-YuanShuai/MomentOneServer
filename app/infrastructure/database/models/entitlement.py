from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
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


class PlanDefinition(Base):
    __tablename__ = "plan_definitions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_plan_definitions_version_positive"),
        CheckConstraint("status IN ('active', 'retired')", name="ck_plan_definitions_status"),
    )

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    entitlements: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    quotas: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UserEntitlement(Base):
    __tablename__ = "user_entitlements"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "entitlement_key",
            "source_type",
            "source_ref",
            name="uq_user_entitlements_source",
        ),
        CheckConstraint(
            "source_type IN ('default', 'admin', 'order', 'subscription', 'promotion')",
            name="ck_user_entitlements_source_type",
        ),
        CheckConstraint(
            "status IN ('active', 'expired', 'revoked')",
            name="ck_user_entitlements_status",
        ),
        CheckConstraint("revision >= 1", name="ck_user_entitlements_revision_positive"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entitlement_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UserStorageAccount(Base):
    __tablename__ = "user_storage_accounts"
    __table_args__ = (
        CheckConstraint("used_bytes >= 0", name="ck_user_storage_used_nonneg"),
        CheckConstraint("reserved_bytes >= 0", name="ck_user_storage_reserved_nonneg"),
        CheckConstraint("effective_quota_bytes >= 0", name="ck_user_storage_quota_nonneg"),
        CheckConstraint("revision >= 1", name="ck_user_storage_revision_positive"),
    )

    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    used_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reserved_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    effective_quota_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    over_quota: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class StorageQuotaGrant(Base):
    __tablename__ = "storage_quota_grants"
    __table_args__ = (
        UniqueConstraint("user_id", "source_type", "source_ref", name="uq_storage_grants_source"),
        CheckConstraint(
            "source_type IN ('default', 'admin', 'order', 'subscription', 'promotion')",
            name="ck_storage_grants_source_type",
        ),
        CheckConstraint(
            "status IN ('active', 'expired', 'revoked')", name="ck_storage_grants_status"
        ),
        CheckConstraint("quota_bytes >= 0", name="ck_storage_grants_quota_nonneg"),
        CheckConstraint("revision >= 1", name="ck_storage_grants_revision_positive"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
