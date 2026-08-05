"""IdempotencyKey 仓储：写请求去重。

支持三种状态：
- processing：请求进行中（重入返回 IN_PROGRESS）
- completed：请求完成，返回缓存的 response_body
- conflict：相同 key 但 request_fingerprint 不同（返回 IDEMPOTENCY_CONFLICT）
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import IdempotencyKey as IdempotencyKeyORM

_DEFAULT_TTL = timedelta(hours=24)


def fingerprint_payload(payload: dict) -> str:
    """对请求体做稳定哈希，用于检测同 key 不同 payload 的冲突。"""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_fingerprint = fingerprint_payload  # 内部别名


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    id: UUID
    user_id: UUID
    operation: str
    idempotency_key: str
    request_fingerprint: str
    state: str
    response_status: int | None
    response_body: dict | None
    resource_id: UUID | None
    created_at: datetime
    expires_at: datetime


def _to_domain(orm: IdempotencyKeyORM) -> IdempotencyRecord:
    return IdempotencyRecord(
        id=orm.id,
        user_id=orm.user_id,
        operation=orm.operation,
        idempotency_key=orm.idempotency_key,
        request_fingerprint=orm.request_fingerprint,
        state=orm.state,
        response_status=orm.response_status,
        response_body=dict(orm.response_body) if orm.response_body else None,
        resource_id=orm.resource_id,
        created_at=orm.created_at,
        expires_at=orm.expires_at,
    )


class SqlIdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire(
        self,
        *,
        user_id: UUID,
        operation: str,
        idempotency_key: str,
        request_payload: dict,
        ttl: timedelta = _DEFAULT_TTL,
    ) -> IdempotencyRecord:
        """获取或创建幂等记录。

        - 不存在 → 创建 processing 记录并返回
        - 存在且 fingerprint 一致 + completed → 返回缓存响应
        - 存在且 fingerprint 一致 + processing → 返回 IN_PROGRESS 状态
        - 存在但 fingerprint 不一致 → 返回 conflict 状态（调用方抛 IDEMPOTENCY_CONFLICT）
        - 存在但已过期 → 视为不存在，更新为 processing
        """
        fingerprint = _fingerprint(request_payload)
        now = datetime.now(UTC)

        stmt = select(IdempotencyKeyORM).where(
            IdempotencyKeyORM.user_id == user_id,
            IdempotencyKeyORM.operation == operation,
            IdempotencyKeyORM.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is not None:
            # 过期视为不存在
            if now > existing.expires_at:
                existing.request_fingerprint = fingerprint
                existing.state = "processing"
                existing.response_status = None
                existing.response_body = None
                existing.resource_id = None
                existing.expires_at = now + ttl
                await self._session.flush()
                return _to_domain(existing)

            if existing.request_fingerprint != fingerprint:
                return _to_domain(existing)  # 调用方判定 conflict

            return _to_domain(existing)

        orm = IdempotencyKeyORM(
            id=uuid4(),
            user_id=user_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            state="processing",
            expires_at=now + ttl,
        )
        self._session.add(orm)
        await self._session.flush()
        return _to_domain(orm)

    async def complete(
        self,
        *,
        record_id: UUID,
        response_status: int,
        response_body: dict,
        resource_id: UUID | None = None,
    ) -> None:
        stmt = select(IdempotencyKeyORM).where(IdempotencyKeyORM.id == record_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if orm is None:
            return
        orm.state = "completed"
        orm.response_status = response_status
        orm.response_body = response_body
        orm.resource_id = resource_id
        await self._session.flush()


__all__ = ["IdempotencyRecord", "SqlIdempotencyRepository", "fingerprint_payload"]
