from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.devices.domain import BindingCode, Device, DeviceBinding


class DeviceRepository(Protocol):
    async def get_or_create(
        self,
        *,
        device_id: str,
        device_type: str | None,
        device_name: str | None,
    ) -> Device: ...

    async def get(self, device_id: str) -> Device | None: ...


class DeviceBindingRepository(Protocol):
    async def create(
        self,
        *,
        user_id: UUID,
        device_id: str,
        scope: tuple[str, ...],
        refresh_token_hash: str,
    ) -> DeviceBinding: ...

    async def get(self, binding_id: UUID) -> DeviceBinding | None: ...

    async def get_by_device(self, device_id: str) -> DeviceBinding | None: ...

    async def list_by_user(self, user_id: UUID) -> list[DeviceBinding]: ...

    async def update_refresh_token_hash(
        self,
        *,
        binding_id: UUID,
        refresh_token_hash: str,
        last_active_at: datetime,
    ) -> None: ...

    async def revoke(self, *, binding_id: UUID, revoked_at: datetime) -> None: ...

    async def update_scope(self, *, binding_id: UUID, scope: tuple[str, ...]) -> DeviceBinding: ...


class BindingCodeRepository(Protocol):
    async def create(
        self,
        *,
        user_id: UUID,
        code: str,
        scope: tuple[str, ...],
        device_name: str | None,
        expires_at: datetime,
    ) -> BindingCode: ...

    async def get_by_code(self, code: str) -> BindingCode | None: ...

    async def mark_used(self, *, code_id: UUID, used_at: datetime) -> None: ...
