from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
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


class QuotaAccount(Base):
    """用户当前周期额度快照；period_start 使用 UTC，非周期额度固定为 Unix epoch。"""

    __tablename__ = "quota_accounts"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "quota_key", "period_start", name="pk_quota_accounts"),
        CheckConstraint("limit_value >= 0", name="ck_quota_accounts_limit_nonneg"),
        CheckConstraint("used_value >= 0", name="ck_quota_accounts_used_nonneg"),
        CheckConstraint("reserved_value >= 0", name="ck_quota_accounts_reserved_nonneg"),
        CheckConstraint("revision >= 1", name="ck_quota_accounts_revision_positive"),
    )

    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    quota_key: Mapped[str] = mapped_column(String(128), nullable=False)
    limit_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    used_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class QuotaUsageEvent(Base):
    """只追加的商业额度计量事实。"""

    __tablename__ = "quota_usage_events"
    __table_args__ = (
        UniqueConstraint("user_id", "quota_key", "operation_key", name="uq_quota_usage_operation"),
        CheckConstraint("amount >= 0", name="ck_quota_usage_amount_nonneg"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quota_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operation_key: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(24), nullable=False)
    device_id: Mapped[str | None] = mapped_column(Text)
    client_id: Mapped[str | None] = mapped_column(Text)
    tool_name: Mapped[str | None] = mapped_column(Text, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    metadata_: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class ApiUsageBucket(Base):
    """按 UTC 日、路由模板和 Method 聚合的 API 请求统计。"""

    __tablename__ = "api_usage_buckets"
    __table_args__ = (
        PrimaryKeyConstraint("bucket_start", "route", "method", name="pk_api_usage_buckets"),
        CheckConstraint("request_count >= 0", name="ck_api_usage_requests_nonneg"),
        CheckConstraint("error_count >= 0", name="ck_api_usage_errors_nonneg"),
        CheckConstraint("latency_ms_total >= 0", name="ck_api_usage_latency_nonneg"),
    )

    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    route: Mapped[str] = mapped_column(String(240), nullable=False)
    method: Mapped[str] = mapped_column(String(12), nullable=False)
    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    latency_ms_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
