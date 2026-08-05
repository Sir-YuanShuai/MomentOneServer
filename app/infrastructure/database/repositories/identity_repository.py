"""UserIdentity 仓储：OIDC 身份映射。

upsert 语义：相同 (issuer, subject) 存在则更新 last_seen_at，否则插入。
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import UserIdentity as UserIdentityORM


class SqlUserIdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, *, user_id: UUID, issuer: str, subject: str) -> None:
        stmt = select(UserIdentityORM).where(
            UserIdentityORM.issuer == issuer,
            UserIdentityORM.subject == subject,
        )
        result = await self._session.execute(stmt)
        existing = result.scalar_one_or_none()
        now = datetime.now(UTC)
        if existing is not None:
            existing.user_id = user_id
            existing.last_seen_at = now
            await self._session.flush()
            return
        orm = UserIdentityORM(
            id=uuid4(),
            user_id=user_id,
            issuer=issuer,
            subject=subject,
            last_seen_at=now,
        )
        self._session.add(orm)
        await self._session.flush()


__all__ = ["SqlUserIdentityRepository"]
