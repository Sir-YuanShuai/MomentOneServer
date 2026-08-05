"""Asset + MomentAsset 仓储。

- AssetRepository：assets 表 CRUD + 状态转换
- MomentAssetRepository：moment_assets 关联表写入 + 按 moment 列出

所有查询显式带 user_id，禁止只按全局 UUID 查询后再判断所有权。
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import Asset as AssetORM
from app.infrastructure.database.models import MomentAsset as MomentAssetORM
from app.modules.assets.domain import (
    Asset,
    AssetKind,
    AssetRole,
    AssetState,
    MomentAssetLink,
    build_storage_key,
)


def _orm_to_domain(orm: AssetORM) -> Asset:
    return Asset(
        id=orm.id,
        user_id=orm.user_id,
        state=AssetState(orm.state),
        kind=AssetKind(orm.kind),
        storage_key=orm.storage_key,
        content_type=orm.content_type,
        size_bytes=orm.size_bytes,
        checksum_sha256=orm.checksum_sha256,
        created_at=orm.created_at,
        ready_at=orm.ready_at,
        deleted_at=orm.deleted_at,
    )


class AssetRepository:
    """PostgreSQL-backed Asset repository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: UUID,
        kind: AssetKind,
        content_type: str,
        size_bytes: int | None = None,
    ) -> Asset:
        asset_id = uuid4()
        storage_key = build_storage_key(user_id, asset_id)
        orm = AssetORM(
            id=asset_id,
            user_id=user_id,
            state=AssetState.UPLOADING.value,
            kind=kind.value,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=size_bytes,
        )
        self._session.add(orm)
        await self._session.flush()
        return _orm_to_domain(orm)

    async def get_by_id(self, asset_id: UUID, user_id: UUID) -> Asset | None:
        stmt = select(AssetORM).where(
            and_(
                AssetORM.id == asset_id,
                AssetORM.user_id == user_id,
                AssetORM.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _orm_to_domain(orm) if orm else None

    async def mark_ready(
        self,
        asset_id: UUID,
        user_id: UUID,
        *,
        size_bytes: int,
        checksum_sha256: str | None = None,
    ) -> Asset | None:
        stmt = select(AssetORM).where(
            and_(
                AssetORM.id == asset_id,
                AssetORM.user_id == user_id,
                AssetORM.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            return None
        orm.state = AssetState.READY.value
        orm.size_bytes = size_bytes
        orm.checksum_sha256 = checksum_sha256
        orm.ready_at = datetime.now(UTC)
        await self._session.flush()
        await self._session.refresh(orm)
        return _orm_to_domain(orm)

    async def mark_failed(self, asset_id: UUID, user_id: UUID) -> Asset | None:
        stmt = select(AssetORM).where(
            and_(
                AssetORM.id == asset_id,
                AssetORM.user_id == user_id,
                AssetORM.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            return None
        orm.state = AssetState.FAILED.value
        await self._session.flush()
        await self._session.refresh(orm)
        return _orm_to_domain(orm)


class MomentAssetRepository:
    """moment_assets 关联表仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def attach(
        self,
        *,
        user_id: UUID,
        moment_id: UUID,
        asset_id: UUID,
        position: int,
        role: AssetRole = AssetRole.ORIGINAL,
    ) -> MomentAssetLink:
        orm = MomentAssetORM(
            user_id=user_id,
            moment_id=moment_id,
            asset_id=asset_id,
            position=position,
            role=role.value,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm)
        return MomentAssetLink(
            user_id=orm.user_id,
            moment_id=orm.moment_id,
            asset_id=orm.asset_id,
            position=orm.position,
            role=AssetRole(orm.role),
            created_at=orm.created_at,
        )

    async def list_by_moment(
        self,
        moment_id: UUID,
        user_id: UUID,
    ) -> list[MomentAssetLink]:
        stmt = (
            select(MomentAssetORM)
            .where(
                and_(
                    MomentAssetORM.user_id == user_id,
                    MomentAssetORM.moment_id == moment_id,
                )
            )
            .order_by(MomentAssetORM.position.asc())
        )
        result = await self._session.execute(stmt)
        return [
            MomentAssetLink(
                user_id=orm.user_id,
                moment_id=orm.moment_id,
                asset_id=orm.asset_id,
                position=orm.position,
                role=AssetRole(orm.role),
                created_at=orm.created_at,
            )
            for orm in result.scalars().all()
        ]

    async def detach_all(self, moment_id: UUID, user_id: UUID) -> int:
        """删除某 Moment 的全部关联（用于 Moment 删除时清理）。返回删除行数。"""
        stmt = delete(MomentAssetORM).where(
            and_(
                MomentAssetORM.user_id == user_id,
                MomentAssetORM.moment_id == moment_id,
            )
        )
        result = await self._session.execute(stmt)
        rowcount = result.rowcount if isinstance(result, CursorResult) else 0
        return int(rowcount)


__all__ = ["AssetRepository", "MomentAssetRepository"]
