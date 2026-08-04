from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import (
    BindingCode as BindingCodeORM,
)
from app.infrastructure.database.models import (
    Device as DeviceORM,
)
from app.infrastructure.database.models import (
    DeviceBinding as DeviceBindingORM,
)
from app.modules.devices.domain import (
    BindingCode,
    BindingCodeStatus,
    BindingStatus,
    Device,
    DeviceBinding,
)


def _device_to_domain(orm: DeviceORM) -> Device:
    return Device(
        id=orm.id,
        device_type=orm.device_type,
        device_name=orm.device_name,
        created_at=orm.created_at,
    )


def _binding_to_domain(orm: DeviceBindingORM) -> DeviceBinding:
    return DeviceBinding(
        id=orm.id,
        user_id=orm.user_id,
        device_id=orm.device_id,
        scope=tuple(orm.scope or ()),
        status=BindingStatus(orm.status),
        refresh_token_hash=orm.refresh_token_hash,
        bound_at=orm.bound_at,
        last_active_at=orm.last_active_at,
        revoked_at=orm.revoked_at,
    )


def _code_to_domain(orm: BindingCodeORM) -> BindingCode:
    return BindingCode(
        id=orm.id,
        code=orm.code,
        user_id=orm.user_id,
        scope=tuple(orm.scope or ()),
        device_name=orm.device_name,
        status=BindingCodeStatus(orm.status),
        expires_at=orm.expires_at,
        used_at=orm.used_at,
        created_at=orm.created_at,
    )


class SqlDeviceRepository:
    """设备注册表读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(
        self,
        *,
        device_id: str,
        device_type: str | None,
        device_name: str | None,
    ) -> Device:
        stmt = select(DeviceORM).where(DeviceORM.id == device_id)
        result = await self._session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            # 更新 device_type / device_name（眼镜端可能升级了固件或改名）
            if device_type and device_type != existing.device_type:
                existing.device_type = device_type
            if device_name and device_name != existing.device_name:
                existing.device_name = device_name
            await self._session.flush()
            return _device_to_domain(existing)

        new_device = DeviceORM(
            id=device_id,
            device_type=device_type,
            device_name=device_name,
        )
        self._session.add(new_device)
        await self._session.flush()
        return _device_to_domain(new_device)

    async def get(self, device_id: str) -> Device | None:
        stmt = select(DeviceORM).where(DeviceORM.id == device_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _device_to_domain(orm) if orm else None


class SqlDeviceBindingRepository:
    """设备绑定关系读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: UUID,
        device_id: str,
        scope: tuple[str, ...],
        refresh_token_hash: str,
    ) -> DeviceBinding:
        orm = DeviceBindingORM(
            user_id=user_id,
            device_id=device_id,
            scope=list(scope),
            status=BindingStatus.ACTIVE.value,
            refresh_token_hash=refresh_token_hash,
        )
        self._session.add(orm)
        await self._session.flush()
        return _binding_to_domain(orm)

    async def get(self, binding_id: UUID) -> DeviceBinding | None:
        stmt = select(DeviceBindingORM).where(DeviceBindingORM.id == binding_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _binding_to_domain(orm) if orm else None

    async def get_by_device(self, device_id: str) -> DeviceBinding | None:
        stmt = select(DeviceBindingORM).where(
            DeviceBindingORM.device_id == device_id,
            DeviceBindingORM.status == BindingStatus.ACTIVE.value,
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _binding_to_domain(orm) if orm else None

    async def list_by_user(self, user_id: UUID) -> list[DeviceBinding]:
        stmt = (
            select(DeviceBindingORM)
            .where(DeviceBindingORM.user_id == user_id)
            .order_by(DeviceBindingORM.bound_at.desc())
        )
        result = await self._session.execute(stmt)
        return [_binding_to_domain(orm) for orm in result.scalars().all()]

    async def update_refresh_token_hash(
        self,
        *,
        binding_id: UUID,
        refresh_token_hash: str,
        last_active_at: datetime,
    ) -> None:
        stmt = select(DeviceBindingORM).where(DeviceBindingORM.id == binding_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            return
        orm.refresh_token_hash = refresh_token_hash
        orm.last_active_at = last_active_at
        await self._session.flush()

    async def revoke(self, *, binding_id: UUID, revoked_at: datetime) -> None:
        stmt = select(DeviceBindingORM).where(DeviceBindingORM.id == binding_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            return
        orm.status = BindingStatus.REVOKED.value
        orm.revoked_at = revoked_at
        orm.refresh_token_hash = None
        await self._session.flush()

    async def update_scope(self, *, binding_id: UUID, scope: tuple[str, ...]) -> DeviceBinding:
        stmt = select(DeviceBindingORM).where(DeviceBindingORM.id == binding_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one()
        orm.scope = list(scope)
        await self._session.flush()
        return _binding_to_domain(orm)


class SqlBindingCodeRepository:
    """绑定会话码读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: UUID,
        code: str,
        scope: tuple[str, ...],
        device_name: str | None,
        expires_at: datetime,
    ) -> BindingCode:
        orm = BindingCodeORM(
            user_id=user_id,
            code=code,
            scope=list(scope),
            device_name=device_name,
            status=BindingCodeStatus.PENDING.value,
            expires_at=expires_at,
        )
        self._session.add(orm)
        await self._session.flush()
        return _code_to_domain(orm)

    async def get_by_code(self, code: str) -> BindingCode | None:
        stmt = select(BindingCodeORM).where(BindingCodeORM.code == code)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _code_to_domain(orm) if orm else None

    async def mark_used(self, *, code_id: UUID, used_at: datetime) -> None:
        stmt = select(BindingCodeORM).where(BindingCodeORM.id == code_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            return
        orm.status = BindingCodeStatus.USED.value
        orm.used_at = used_at
        await self._session.flush()


__all__ = [
    "SqlBindingCodeRepository",
    "SqlDeviceBindingRepository",
    "SqlDeviceRepository",
]

# 避免未使用导入告警（timezone 用于后续扩展）
_ = timezone
