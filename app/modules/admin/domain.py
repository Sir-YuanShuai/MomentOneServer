from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AdminUser:
    id: UUID
    display_name: str | None
    email: str | None
    casdoor_sub: str
    status: str
    revision: int
    created_at: datetime
    updated_at: datetime
    last_active_at: datetime | None
    disabled_at: datetime | None
    disable_reason: str | None
    moment_count: int
    active_binding_count: int
    active_mcp_count: int


@dataclass(frozen=True, slots=True)
class AdminBinding:
    id: UUID
    user_id: UUID
    user_display_name: str | None
    user_email: str | None
    device_id: str
    device_name: str | None
    scope: tuple[str, ...]
    status: str
    revision: int
    bound_at: datetime
    last_active_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class AdminAuthorization:
    id: UUID
    user_id: UUID
    user_display_name: str | None
    user_email: str | None
    client_id: str
    client_name: str | None
    client_type: str
    scope: tuple[str, ...]
    status: str
    revision: int
    created_at: datetime
    updated_at: datetime
    last_active_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class AdminAuditEvent:
    id: UUID
    user_id: UUID | None
    actor_type: str
    actor_id: str | None
    event_type: str
    resource_type: str | None
    resource_id: UUID | None
    request_id: str | None
    allowed: bool
    reason: str | None
    metadata: dict[str, object]
    created_at: datetime
