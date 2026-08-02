from datetime import UTC, datetime
from uuid import UUID

import pytest
from app.core.errors import ApplicationError
from app.modules.moments.domain import Moment, MomentCategory
from app.modules.moments.service import MomentService

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
MOMENT_ID = UUID("22222222-2222-4222-8222-222222222222")


class FakeMomentRepository:
    def __init__(self, moment: Moment | None) -> None:
        self._moment = moment

    async def get(self, *, user_id: UUID, moment_id: UUID) -> Moment | None:
        if self._moment and self._moment.user_id == user_id and self._moment.id == moment_id:
            return self._moment
        return None


@pytest.mark.asyncio
async def test_get_moment() -> None:
    timestamp = datetime(2026, 8, 1, tzinfo=UTC)
    moment = Moment(
        id=MOMENT_ID,
        user_id=USER_ID,
        title="第一次带妈妈看海",
        description="今天第一次带妈妈看海。",
        voice_input="今天第一次带妈妈看海",
        ai_summary="第一次带妈妈看海。",
        category=MomentCategory.EXPERIENCE,
        tags=("家人", "大海"),
        occurred_at=timestamp,
        timezone="Asia/Shanghai",
        revision=1,
        created_at=timestamp,
        updated_at=timestamp,
    )
    service = MomentService(FakeMomentRepository(moment))

    result = await service.get(user_id=USER_ID, moment_id=MOMENT_ID)

    assert result == moment


@pytest.mark.asyncio
async def test_get_missing_moment() -> None:
    service = MomentService(FakeMomentRepository(None))

    with pytest.raises(ApplicationError) as raised:
        await service.get(user_id=USER_ID, moment_id=MOMENT_ID)

    assert raised.value.code == "MOMENT_NOT_FOUND"
    assert raised.value.status_code == 404
