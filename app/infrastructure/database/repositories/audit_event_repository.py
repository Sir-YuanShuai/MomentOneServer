"""AuditEvent 仓储：只追加的安全与业务审计流。

调用方必须确保 metadata 已脱敏，不含 Token/凭据/完整私密正文。
"""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import AuditEvent as AuditEventORM


class SqlAuditEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        user_id: UUID | None,
        actor_type: str,
        actor_id: str | None,
        event_type: str,
        resource_type: str | None = None,
        resource_id: UUID | None = None,
        request_id: str | None = None,
        allowed: bool,
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        orm = AuditEventORM(
            id=uuid4(),
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=request_id,
            allowed=allowed,
            reason=reason,
            metadata_=metadata or {},
        )
        self._session.add(orm)
        await self._session.flush()


__all__ = ["SqlAuditEventRepository"]
