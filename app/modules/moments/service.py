from uuid import UUID

from app.core.errors import ApplicationError
from app.modules.moments.domain import Moment
from app.modules.moments.repository import MomentRepository


class MomentService:
    def __init__(self, repository: MomentRepository) -> None:
        self._repository = repository

    async def get(self, *, user_id: UUID, moment_id: UUID) -> Moment:
        moment = await self._repository.get(user_id=user_id, moment_id=moment_id)
        if moment is None:
            raise ApplicationError(
                code="MOMENT_NOT_FOUND",
                message="Moment 不存在。",
                status_code=404,
            )
        return moment
