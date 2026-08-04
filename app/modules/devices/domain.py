from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class BindingStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class BindingCodeStatus(StrEnum):
    PENDING = "pending"
    USED = "used"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    device_type: str | None
    device_name: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DeviceBinding:
    id: UUID
    user_id: UUID
    device_id: str
    scope: tuple[str, ...]
    status: BindingStatus
    refresh_token_hash: str | None
    bound_at: datetime
    last_active_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class BindingCode:
    id: UUID
    code: str
    user_id: UUID
    scope: tuple[str, ...]
    device_name: str | None
    status: BindingCodeStatus
    expires_at: datetime
    used_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TokenResponse:
    """OAuth 2.1 Token 端点响应。"""

    binding_id: UUID
    access_token: str
    refresh_token: str
    token_type: str  # 固定 "Bearer"
    expires_in: int  # access_token 有效期（秒）
    scope: str  # 空格分隔的 scope 字符串
