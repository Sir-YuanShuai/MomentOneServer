import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.infrastructure.database.models.notification import (
    InAppNotification,
    NotificationDelivery,
    NotificationJob,
    NotificationPreference,
    OutboxEvent,
    Reminder,
)
from app.infrastructure.database.models.push_subscription import PushSubscription
from app.infrastructure.database.session import get_database
from app.modules.notifications.push import PushSecretCipher, PushSecrets, WebPushSender

logger = structlog.get_logger()


class NotificationWorker:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    async def start(self) -> None:
        if not self._settings.notification_worker_enabled:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="notification-worker")
            await logger.ainfo(
                "notification_worker_started", worker_id=self._settings.notification_worker_id
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                handled_events, handled_jobs = await self.run_once()
                if handled_events or handled_jobs:
                    continue
            except Exception:
                await logger.aexception("notification_worker_cycle_failed")
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._settings.notification_poll_interval_seconds,
                )
                self._wake.clear()
            except TimeoutError:
                pass

    async def run_once(self) -> tuple[int, int]:
        """Process one bounded batch; exposed for health checks and integration tests."""
        handled_events = await self._process_outbox()
        handled_jobs = await self._process_jobs()
        return handled_events, handled_jobs

    async def _process_outbox(self) -> int:
        async with get_database().session_factory() as session, session.begin():
            now = datetime.now(UTC)
            result = await session.execute(
                select(OutboxEvent)
                .where(
                    OutboxEvent.processed_at.is_(None),
                    OutboxEvent.available_at <= now,
                )
                .order_by(OutboxEvent.occurred_at)
                .limit(self._settings.notification_batch_size)
                .with_for_update(skip_locked=True)
            )
            events = list(result.scalars())
            for event in events:
                try:
                    await self._apply_event(session, event)
                    event.processed_at = now
                    event.last_error = None
                except Exception as exc:
                    event.attempt_count += 1
                    event.last_error = str(exc)[:1000]
                    event.available_at = now + timedelta(
                        seconds=min(300, 2 ** min(event.attempt_count, 8))
                    )
            return len(events)

    async def _apply_event(self, session: AsyncSession, event: OutboxEvent) -> None:
        if event.aggregate_type != "reminder" or event.user_id is None:
            return
        await session.execute(
            update(NotificationJob)
            .where(
                NotificationJob.source_type == "reminder",
                NotificationJob.source_id == event.aggregate_id,
                NotificationJob.status.in_(["pending", "retry"]),
            )
            .values(status="cancelled", locked_at=None, locked_by=None)
        )
        if event.event_type not in {"reminder.created", "reminder.rescheduled"}:
            return
        due_at_raw = event.payload.get("dueAt")
        if not isinstance(due_at_raw, str):
            return
        due_at = datetime.fromisoformat(due_at_raw.replace("Z", "+00:00"))
        dedup = f"reminder:{event.aggregate_id}:revision:{event.aggregate_revision}:due"
        exists = await session.scalar(
            select(NotificationJob.id).where(NotificationJob.deduplication_key == dedup)
        )
        if exists is None:
            session.add(
                NotificationJob(
                    id=uuid4(),
                    user_id=event.user_id,
                    job_type="reminder_due",
                    source_type="reminder",
                    source_id=event.aggregate_id,
                    source_revision=event.aggregate_revision,
                    scheduled_at=due_at,
                    status="pending",
                    deduplication_key=dedup,
                )
            )

    async def _process_jobs(self) -> int:
        handled = 0
        for _ in range(self._settings.notification_batch_size):
            job_id = await self._claim_job()
            if job_id is None:
                break
            handled += 1
            try:
                await self._deliver_job(job_id)
            except Exception:
                await logger.aexception("notification_job_failed", job_id=str(job_id))
                await self._retry_job(job_id, "unexpected worker failure")
        return handled

    async def _claim_job(self) -> UUID | None:
        async with get_database().session_factory() as session, session.begin():
            now = datetime.now(UTC)
            result = await session.execute(
                select(NotificationJob)
                .where(
                    NotificationJob.status.in_(["pending", "retry"]),
                    NotificationJob.scheduled_at <= now,
                    or_(
                        NotificationJob.next_attempt_at.is_(None),
                        NotificationJob.next_attempt_at <= now,
                    ),
                )
                .order_by(NotificationJob.scheduled_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            job = result.scalar_one_or_none()
            if job is None:
                return None
            job.status = "running"
            job.locked_at = now
            job.locked_by = self._settings.notification_worker_id
            return job.id

    async def _deliver_job(self, job_id: UUID) -> None:
        async with get_database().session_factory() as session, session.begin():
            job = await session.get(NotificationJob, job_id, with_for_update=True)
            if job is None or job.status != "running":
                return
            reminder = await session.scalar(
                select(Reminder).where(
                    Reminder.id == UUID(job.source_id), Reminder.user_id == job.user_id
                )
            )
            if (
                reminder is None
                or reminder.deleted_at is not None
                or reminder.status != "pending"
                or reminder.revision != job.source_revision
            ):
                job.status = "skipped"
                job.locked_at = None
                job.locked_by = None
                return
            preference = await session.get(NotificationPreference, job.user_id)
            if preference is not None and not preference.reminders_enabled:
                job.status = "skipped"
                job.locked_at = None
                job.locked_by = None
                return
            quiet_hours_end = self._quiet_hours_end(preference)
            if quiet_hours_end is not None:
                job.status = "retry"
                job.next_attempt_at = quiet_hours_end
                job.locked_at = None
                job.locked_by = None
                return
            notification = await session.scalar(
                select(InAppNotification).where(
                    InAppNotification.deduplication_key == job.deduplication_key
                )
            )
            if notification is None:
                notification = InAppNotification(
                    id=uuid4(),
                    user_id=job.user_id,
                    category="reminder",
                    title="一刻提醒",
                    body=reminder.title[:160],
                    target=f"/space/reminders/?reminder={reminder.id}",
                    tag=f"reminder-{reminder.id}",
                    deduplication_key=job.deduplication_key,
                    source_type="reminder",
                    source_id=str(reminder.id),
                )
                session.add(notification)
                await session.flush()
            if preference is not None and not preference.web_push_enabled:
                job.status = "sent"
                job.locked_at = None
                job.locked_by = None
                return
            subscriptions = list(
                (
                    await session.execute(
                        select(PushSubscription).where(
                            PushSubscription.user_id == job.user_id,
                            PushSubscription.status == "active",
                        )
                    )
                ).scalars()
            )
            retriable_failure = False
            for subscription in subscriptions:
                delivery = await session.scalar(
                    select(NotificationDelivery).where(
                        NotificationDelivery.notification_id == notification.id,
                        NotificationDelivery.channel == "web_push",
                        NotificationDelivery.target_id == subscription.id,
                    )
                )
                if delivery is not None and delivery.status == "accepted":
                    continue
                delivery = delivery or NotificationDelivery(
                    id=uuid4(),
                    notification_id=notification.id,
                    user_id=job.user_id,
                    channel="web_push",
                    target_id=subscription.id,
                    status="pending",
                )
                session.add(delivery)
                delivery.attempt_count += 1
                try:
                    await self._sender().send_payload(
                        subscription=self._secrets(subscription),
                        payload={
                            "version": 1,
                            "notificationId": str(notification.id),
                            "title": notification.title,
                            "body": (
                                notification.body
                                if preference is not None
                                and preference.lock_screen_detail == "full"
                                else "你有一项待处理提醒"
                            ),
                            "target": notification.target,
                            "tag": notification.tag,
                        },
                        ttl=3600,
                    )
                    delivery.status = "accepted"
                    delivery.accepted_at = datetime.now(UTC)
                    delivery.last_error = None
                    subscription.last_accepted_at = datetime.now(UTC)
                    subscription.failure_count = 0
                except ApplicationError as exc:
                    delivery.last_error = exc.code
                    subscription.failure_count += 1
                    if exc.code == "PUSH_SUBSCRIPTION_EXPIRED":
                        delivery.status = "expired"
                        subscription.status = "revoked"
                        subscription.revoked_at = datetime.now(UTC)
                    else:
                        delivery.status = "failed"
                        retriable_failure = True
            if retriable_failure and job.attempt_count < 5:
                job.attempt_count += 1
                job.status = "retry"
                job.next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=min(300, 2**job.attempt_count * 5)
                )
            else:
                job.status = "sent" if not retriable_failure else "failed"
            job.locked_at = None
            job.locked_by = None

    async def _retry_job(self, job_id: UUID, error: str) -> None:
        async with get_database().session_factory() as session, session.begin():
            job = await session.get(NotificationJob, job_id)
            if job is None:
                return
            job.attempt_count += 1
            job.last_error = error
            job.status = "retry" if job.attempt_count < 5 else "failed"
            job.next_attempt_at = datetime.now(UTC) + timedelta(
                seconds=min(300, 2 ** min(job.attempt_count, 8) * 5)
            )
            job.locked_at = None
            job.locked_by = None

    def _sender(self) -> WebPushSender:
        return WebPushSender(self._settings)

    @staticmethod
    def _quiet_hours_end(preference: NotificationPreference | None) -> datetime | None:
        if (
            preference is None
            or not preference.quiet_hours_enabled
            or preference.quiet_hours_start is None
            or preference.quiet_hours_end is None
        ):
            return None
        try:
            timezone = ZoneInfo(preference.timezone)
        except ZoneInfoNotFoundError:
            timezone = ZoneInfo("UTC")
        now = datetime.now(timezone)
        start = preference.quiet_hours_start
        end = preference.quiet_hours_end
        inside = (
            start <= now.time() < end if start < end else now.time() >= start or now.time() < end
        )
        if not inside:
            return None
        end_date = now.date()
        if start >= end and now.time() >= start:
            end_date += timedelta(days=1)
        return datetime.combine(end_date, end, timezone).astimezone(UTC)

    def _secrets(self, subscription: PushSubscription) -> PushSecrets:
        key = self._settings.web_push_subscription_encryption_key
        if not key:
            raise ApplicationError(
                code="WEB_PUSH_DISABLED", message="系统通知暂未启用。", status_code=503
            )
        cipher = PushSecretCipher(key)
        return PushSecrets(
            endpoint=cipher.decrypt(subscription.endpoint_encrypted),
            p256dh=cipher.decrypt(subscription.p256dh_encrypted),
            auth=cipher.decrypt(subscription.auth_encrypted),
        )
