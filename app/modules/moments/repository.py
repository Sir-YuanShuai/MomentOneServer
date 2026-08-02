from typing import Protocol
from uuid import UUID

from app.modules.moments.domain import Moment


class MomentRepository(Protocol):
    async def get(self, *, user_id: UUID, moment_id: UUID) -> Moment | None: ...
