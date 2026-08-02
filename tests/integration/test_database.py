import pytest
from app.core.config import Settings
from app.infrastructure.database.session import Database
from sqlalchemy import text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgresql_connection() -> None:
    settings = Settings()
    if not settings.database_url:
        pytest.skip("MOMENT_ONE_DATABASE_URL is not configured")

    database = Database(settings)
    try:
        async with database.session_factory() as session:
            result = await session.scalar(text("SELECT 1"))
        assert result == 1
    finally:
        await database.dispose()
