from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.push_subscription import PushSubscription


class PushSubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_user(self, user_id: UUID) -> list[PushSubscription]:
        result = await self._session.execute(
            select(PushSubscription)
            .where(PushSubscription.user_id == user_id)
            .order_by(PushSubscription.created_at.desc())
        )
        return list(result.scalars())

    async def get(self, subscription_id: UUID, user_id: UUID) -> PushSubscription | None:
        result = await self._session.execute(
            select(PushSubscription).where(
                PushSubscription.id == subscription_id,
                PushSubscription.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_hash(self, value: str) -> PushSubscription | None:
        result = await self._session.execute(
            select(PushSubscription).where(PushSubscription.endpoint_hash == value)
        )
        return result.scalar_one_or_none()

    async def save(self, subscription: PushSubscription) -> PushSubscription:
        self._session.add(subscription)
        await self._session.flush()
        return subscription

    async def revoke(self, subscription: PushSubscription) -> None:
        subscription.status = "revoked"
        subscription.revoked_at = datetime.now(UTC)
        subscription.updated_at = datetime.now(UTC)
        await self._session.flush()
