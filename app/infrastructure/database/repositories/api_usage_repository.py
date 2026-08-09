from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import ApiUsageBucket


class ApiUsageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        route: str,
        method: str,
        status_code: int,
        latency_ms: int,
        occurred_at: datetime | None = None,
    ) -> None:
        now = (occurred_at or datetime.now(UTC)).astimezone(UTC)
        bucket_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        stmt = insert(ApiUsageBucket).values(
            bucket_start=bucket_start,
            route=route[:240],
            method=method[:12],
            request_count=1,
            error_count=1 if status_code >= 400 else 0,
            latency_ms_total=max(0, latency_ms),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                ApiUsageBucket.bucket_start,
                ApiUsageBucket.route,
                ApiUsageBucket.method,
            ],
            set_={
                "request_count": ApiUsageBucket.request_count + 1,
                "error_count": ApiUsageBucket.error_count + (1 if status_code >= 400 else 0),
                "latency_ms_total": ApiUsageBucket.latency_ms_total + max(0, latency_ms),
                "updated_at": now,
            },
        )
        await self._session.execute(stmt)
        await self._session.flush()
