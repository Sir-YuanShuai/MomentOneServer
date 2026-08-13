from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models.user_feedback import UserFeedback


class UserFeedbackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, item: UserFeedback) -> UserFeedback:
        self._session.add(item)
        await self._session.flush()
        return item


__all__ = ["UserFeedbackRepository"]
