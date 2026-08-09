import asyncio
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.infrastructure.database.models import ApiUsageBucket
from app.infrastructure.database.session import get_database, init_database
from app.modules.usage.recorder import ApiUsageMetric, ApiUsageRecorder
from sqlalchemy import func, select, text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_usage_recorder_does_not_need_a_second_request_connection() -> None:
    """A pool of one must not deadlock while a request still owns its connection."""
    settings = Settings(database_pool_size=1, database_max_overflow=0)
    if not settings.database_url:
        pytest.skip("MOMENT_ONE_DATABASE_URL is not configured")

    database = init_database(settings)
    recorder = ApiUsageRecorder()
    route = f"/v1/integration/pool-{uuid4()}"
    await recorder.start()
    try:
        async with database.session_factory() as request_session:
            # Simulate the request-scoped dependency transaction that remains open
            # until after middleware has produced the response.
            await request_session.execute(text("SELECT 1"))
            assert recorder.submit(
                ApiUsageMetric(
                    route=route,
                    method="GET",
                    status_code=200,
                    latency_ms=7,
                    request_id=str(uuid4()),
                )
            )
            # The background worker is waiting for the only pooled connection, but
            # submission and response completion are not blocked by that wait.
            await asyncio.sleep(0.05)
            await request_session.rollback()

        assert await recorder.flush(wait_seconds=2)
        async with get_database().session_factory() as session:
            count = await session.scalar(
                select(func.sum(ApiUsageBucket.request_count)).where(
                    ApiUsageBucket.route == route,
                    ApiUsageBucket.method == "GET",
                )
            )
            assert count == 1
            await session.rollback()
    finally:
        await recorder.stop()
        await database.dispose()
