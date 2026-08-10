from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import UserIdentity


class IdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_external(
        self, issuer: str, subject: str, *, active_only: bool = True
    ) -> UserIdentity | None:
        stmt = select(UserIdentity).where(
            UserIdentity.issuer == issuer,
            UserIdentity.subject == subject,
        )
        if active_only:
            stmt = stmt.where(UserIdentity.status == "active")
        return await self._session.scalar(stmt)

    async def get(self, identity_id: UUID) -> UserIdentity | None:
        return await self._session.scalar(
            select(UserIdentity).where(UserIdentity.id == identity_id)
        )

    async def list_for_user(self, user_id: UUID, *, active_only: bool = True) -> list[UserIdentity]:
        stmt = select(UserIdentity).where(UserIdentity.user_id == user_id)
        if active_only:
            stmt = stmt.where(UserIdentity.status == "active")
        stmt = stmt.order_by(UserIdentity.is_primary.desc(), UserIdentity.created_at.asc())
        return list((await self._session.execute(stmt)).scalars().all())

    async def active_count(self, user_id: UUID) -> int:
        return int(
            await self._session.scalar(
                select(func.count(UserIdentity.id)).where(
                    UserIdentity.user_id == user_id,
                    UserIdentity.status == "active",
                    UserIdentity.identity_type.in_(("oidc", "email", "phone", "provider")),
                )
            )
            or 0
        )

    async def ensure_oidc(
        self,
        *,
        user_id: UUID,
        issuer: str,
        subject: str,
        identifier: str | None,
        display_name: str | None,
        provider: str = "casdoor",
        primary_if_first: bool = True,
        metadata: dict[str, object] | None = None,
    ) -> UserIdentity:
        existing = await self.get_by_external(issuer, subject, active_only=False)
        if existing is not None:
            return await self._touch_oidc(
                existing,
                user_id=user_id,
                identifier=identifier,
                display_name=display_name,
                metadata=metadata,
            )
        now = datetime.now(UTC)
        is_primary = primary_if_first and await self.active_count(user_id) == 0
        identity = UserIdentity(
            user_id=user_id,
            issuer=issuer,
            subject=subject,
            identity_type="oidc",
            provider=provider,
            identifier=identifier,
            display_name=display_name,
            status="active",
            is_primary=is_primary,
            verified_at=now,
            last_seen_at=now,
            metadata_=metadata or {},
            revision=1,
        )
        try:
            # 两个并发请求可能同时完成“查不到再插入”。用保存点承接唯一键冲突，
            # 让外层认证事务保持可用，再读取已经提交的身份记录。
            async with self._session.begin_nested():
                self._session.add(identity)
                await self._session.flush()
        except IntegrityError:
            existing = await self.get_by_external(issuer, subject, active_only=False)
            if existing is None:
                raise
            return await self._touch_oidc(
                existing,
                user_id=user_id,
                identifier=identifier,
                display_name=display_name,
                metadata=metadata,
            )
        return identity

    async def _touch_oidc(
        self,
        existing: UserIdentity,
        *,
        user_id: UUID,
        identifier: str | None,
        display_name: str | None,
        metadata: dict[str, object] | None,
    ) -> UserIdentity:
        if existing.user_id != user_id:
            return existing
        existing.status = "active"
        existing.last_seen_at = datetime.now(UTC)
        existing.identifier = identifier or existing.identifier
        existing.display_name = display_name or existing.display_name
        existing.metadata_ = {**(existing.metadata_ or {}), **(metadata or {})}
        existing.revision += 1
        await self._session.flush()
        return existing

    async def unlink_other_contacts(self, *, user_id: UUID, kind: str, keep_subject: str) -> None:
        rows = await self.list_for_user(user_id)
        now = datetime.now(UTC)
        for item in rows:
            if item.identity_type == kind and item.subject != keep_subject:
                item.status = "unlinked"
                item.is_primary = False
                item.revision += 1
                item.updated_at = now
        await self._session.flush()

    async def upsert_contact(
        self,
        *,
        user_id: UUID,
        kind: str,
        destination: str,
        primary: bool = False,
    ) -> UserIdentity:
        existing = await self.get_by_external(kind, destination, active_only=False)
        now = datetime.now(UTC)
        if existing is not None:
            if existing.user_id != user_id:
                return existing
            existing.status = "active"
            existing.verified_at = now
            existing.last_seen_at = now
            existing.identifier = destination
            existing.revision += 1
            await self._session.flush()
            return existing
        identity = UserIdentity(
            user_id=user_id,
            issuer=kind,
            subject=destination,
            identity_type=kind,
            provider=kind,
            identifier=destination,
            display_name=destination,
            status="active",
            is_primary=primary,
            verified_at=now,
            last_seen_at=now,
            metadata_={},
            revision=1,
        )
        self._session.add(identity)
        await self._session.flush()
        return identity

    async def unlink(self, identity: UserIdentity) -> None:
        identity.status = "unlinked"
        identity.is_primary = False
        identity.revision += 1
        identity.updated_at = datetime.now(UTC)
        if not any(item.is_primary for item in await self.list_for_user(identity.user_id)):
            replacement = await self._session.scalar(
                select(UserIdentity)
                .where(
                    UserIdentity.user_id == identity.user_id,
                    UserIdentity.status == "active",
                    UserIdentity.id != identity.id,
                )
                .order_by(UserIdentity.created_at.asc())
                .limit(1)
            )
            if replacement is not None:
                replacement.is_primary = True
                replacement.revision += 1
        await self._session.flush()


__all__ = ["IdentityRepository"]
