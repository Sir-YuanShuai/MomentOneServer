from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class Asset(Base):
    """媒体业务元数据；实际字节存放在 MinIO/S3。

    状态转换：uploading -> ready | failed；ready -> detached；failed/detached -> purged。
    只有 state=ready 的 Asset 才能被 Moment 引用。
    """

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_assets_user_id_id"),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        CheckConstraint(
            "state IN ('uploading', 'ready', 'detached', 'failed', 'purged')",
            name="ck_assets_state",
        ),
        CheckConstraint("kind IN ('image', 'audio', 'video', 'document')", name="ck_assets_kind"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_assets_size_nonneg"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="uploading")
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MomentAsset(Base):
    """Moment 与 Asset 的有序关联。

    复合外键 (user_id, moment_id) -> moments(user_id, id) 和
    (user_id, asset_id) -> assets(user_id, id) 由数据库强制，
    阻止 Moment 关联其他用户的 Asset。
    """

    __tablename__ = "moment_assets"
    __table_args__ = (
        PrimaryKeyConstraint("moment_id", "asset_id", name="pk_moment_assets"),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["user_id", "moment_id"], ["moments.user_id", "moments.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["user_id", "asset_id"], ["assets.user_id", "assets.id"], ondelete="CASCADE"
        ),
        CheckConstraint("position >= 0", name="ck_moment_assets_position_nonneg"),
        CheckConstraint(
            "role IN ('original', 'cover', 'voice_note', 'attachment')",
            name="ck_moment_assets_role",
        ),
        UniqueConstraint("moment_id", "position", name="uq_moment_assets_moment_position"),
    )

    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    moment_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    asset_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="original")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
