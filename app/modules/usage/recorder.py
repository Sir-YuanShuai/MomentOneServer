from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from uuid import UUID, uuid4

import structlog

from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.api_usage_repository import ApiUsageRepository
from app.infrastructure.database.session import get_database
from app.modules.quotas.repository import QuotaRepository

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class ApiUsageMetric:
    route: str
    method: str
    status_code: int
    latency_ms: int
    user_id: UUID | None = None
    actor_type: str = "web"
    client_id: str | None = None
    device_id: str | None = None
    request_id: str | None = None


class ApiUsageRecorder:
    """Serialize usage writes through one background worker.

    FastAPI yield dependencies keep the request transaction open until the
    response completes. Opening a second database session inside middleware
    before returning the response can therefore exhaust the whole pool: every
    request holds one connection while waiting for another. The queue lets the
    response finish and release its request-scoped connection first.
    """

    def __init__(self, *, max_queue_size: int = 4096) -> None:
        self._queue: asyncio.Queue[ApiUsageMetric] = asyncio.Queue(maxsize=max_queue_size)
        self._worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="api-usage-recorder")

    async def flush(self, *, wait_seconds: float = 5) -> bool:
        try:
            await asyncio.wait_for(self._queue.join(), timeout=wait_seconds)
            return True
        except TimeoutError:
            await logger.awarning("api_usage_drain_timeout", pending=self._queue.qsize())
            return False

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        await self.flush()
        self._worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._worker_task
        self._worker_task = None

    def submit(self, metric: ApiUsageMetric) -> bool:
        try:
            self._queue.put_nowait(metric)
            return True
        except asyncio.QueueFull:
            logger.warning("api_usage_queue_full", route=metric.route)
            return False

    async def _worker(self) -> None:
        while True:
            metric = await self._queue.get()
            try:
                await self._record(metric)
            except Exception:
                await logger.aexception("api_usage_record_failed", route=metric.route)
            finally:
                self._queue.task_done()

    @staticmethod
    async def _record(metric: ApiUsageMetric) -> None:
        async with get_database().session_factory() as session:
            await ApiUsageRepository(session).record(
                route=metric.route,
                method=metric.method,
                status_code=metric.status_code,
                latency_ms=metric.latency_ms,
            )
            if metric.user_id is not None and not metric.route.startswith("/v1/admin/"):
                try:
                    await QuotaRepository(session).consume(
                        metric.user_id,
                        "api.requests.month",
                        amount=1,
                        operation_key=f"api:{metric.request_id or uuid4()}",
                        actor_type=metric.actor_type,
                        client_id=metric.client_id,
                        device_id=metric.device_id,
                        metadata={
                            "route": metric.route,
                            "method": metric.method,
                            "statusCode": metric.status_code,
                        },
                    )
                except ApplicationError as exc:
                    if exc.code != "QUOTA_EXCEEDED":
                        raise
            await session.commit()


__all__ = ["ApiUsageMetric", "ApiUsageRecorder"]
