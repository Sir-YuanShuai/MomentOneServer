from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Float, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class User(Base):
    """本地用户表，通过 casdoor_sub 与 Casdoor 关联。

    仅存储本项目特有的业务字段；公共身份信息（头像、手机号等）由 Casdoor 管理。
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    casdoor_sub: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    casdoor_user_id: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Moment(Base):
    """Moment 记录表。"""

    __tablename__ = "moments"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), index=True)
    title: Mapped[str] = mapped_column(String(20))
    description: Mapped[str | None] = mapped_column(Text)
    voice_input: Mapped[str | None] = mapped_column(Text)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(20))
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    persons: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    event_name: Mapped[str | None] = mapped_column(String(50))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(50))
    location_name: Mapped[str | None] = mapped_column(String(200))
    location_latitude: Mapped[float | None] = mapped_column(Float)
    location_longitude: Mapped[float | None] = mapped_column(Float)
    location_source: Mapped[str | None] = mapped_column(String(20))
    emotion_label: Mapped[str | None] = mapped_column(String(50))
    emotion_score: Mapped[float | None] = mapped_column(Float)
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 记录类型（D2/D3）：内置类型注册表驱动，general 兜底；payload 为类型化扩展字段
    moment_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="general", server_default="general"
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
