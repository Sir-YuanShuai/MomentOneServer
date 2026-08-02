from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.modules.moments.domain import (
    LocationSource,
    Moment,
    MomentCategory,
    MomentEmotion,
    MomentLocation,
)


class InMemoryMomentRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, Moment] = {}
        self._idempotency: dict[tuple[UUID, str], UUID] = {}

    async def get(self, *, user_id: UUID, moment_id: UUID) -> Moment | None:
        moment = self._store.get(moment_id)
        if moment is None or moment.user_id != user_id:
            return None
        return moment

    async def list(
        self,
        *,
        user_id: UUID,
        limit: int = 20,
        cursor: str | None = None,
        category: str | None = None,
        tag: str | None = None,
        query: str | None = None,
    ) -> tuple[list[Moment], str | None]:
        items = [
            m
            for m in self._store.values()
            if m.user_id == user_id and m.deleted_at is None
        ]
        if category:
            items = [m for m in items if m.category.value == category]
        if tag:
            items = [m for m in items if tag in m.tags]
        if query:
            q = query.lower()
            items = [
                m
                for m in items
                if q in m.title.lower()
                or (m.description and q in m.description.lower())
                or (m.ai_summary and q in m.ai_summary.lower())
                or any(q in t.lower() for t in m.tags)
            ]
        items.sort(key=lambda m: m.occurred_at, reverse=True)

        start_idx = 0
        if cursor:
            for i, m in enumerate(items):
                if str(m.id) == cursor:
                    start_idx = i + 1
                    break

        page = items[start_idx : start_idx + limit]
        next_cursor = str(page[-1].id) if len(page) == limit and start_idx + limit < len(items) else None
        return page, next_cursor

    async def create(
        self,
        *,
        user_id: UUID,
        title: str,
        description: str | None = None,
        voice_input: str | None = None,
        ai_summary: str | None = None,
        category: str = "experience",
        tags: list[str] | None = None,
        occurred_at: datetime | None = None,
        timezone: str = "UTC",
        location: dict | None = None,
        emotion: dict | None = None,
        idempotency_key: str | None = None,
    ) -> Moment:
        if idempotency_key:
            existing = self._idempotency.get((user_id, idempotency_key))
            if existing:
                return self._store[existing]

        now = datetime.now(UTC)
        moment = Moment(
            id=uuid4(),
            user_id=user_id,
            title=title,
            description=description,
            voice_input=voice_input,
            ai_summary=ai_summary,
            category=MomentCategory(category),
            tags=tuple(tags or []),
            occurred_at=occurred_at or now,
            timezone=timezone,
            location=self._parse_location(location),
            emotion=self._parse_emotion(emotion),
            revision=1,
            created_at=now,
            updated_at=now,
        )
        self._store[moment.id] = moment
        if idempotency_key:
            self._idempotency[(user_id, idempotency_key)] = moment.id
        return moment

    async def update(
        self,
        *,
        user_id: UUID,
        moment_id: UUID,
        expected_revision: int,
        changes: dict,
    ) -> Moment:
        moment = await self.get(user_id=user_id, moment_id=moment_id)
        if moment is None:
            return None  # type: ignore[return-value]

        if moment.revision != expected_revision:
            from app.core.errors import ApplicationError

            raise ApplicationError(
                code="REVISION_CONFLICT",
                message="Moment 已被其他操作修改，请刷新后重试。",
                status_code=409,
                details={
                    "expectedRevision": expected_revision,
                    "actualRevision": moment.revision,
                },
            )

        now = datetime.now(UTC)
        updated = Moment(
            id=moment.id,
            user_id=moment.user_id,
            title=changes.get("title", moment.title),
            description=changes.get("description", moment.description),
            voice_input=changes.get("voiceInput", moment.voice_input),
            ai_summary=changes.get("aiSummary", moment.ai_summary),
            category=MomentCategory(changes["category"]) if "category" in changes else moment.category,
            tags=tuple(changes["tags"]) if "tags" in changes else moment.tags,
            occurred_at=moment.occurred_at,
            timezone=changes.get("timezone", moment.timezone),
            location=self._parse_location(changes["location"]) if "location" in changes else moment.location,
            emotion=self._parse_emotion(changes["emotion"]) if "emotion" in changes else moment.emotion,
            revision=moment.revision + 1,
            created_at=moment.created_at,
            updated_at=now,
        )
        self._store[moment.id] = updated
        return updated

    async def soft_delete(self, *, user_id: UUID, moment_id: UUID) -> Moment | None:
        moment = await self.get(user_id=user_id, moment_id=moment_id)
        if moment is None:
            return None
        now = datetime.now(UTC)
        deleted = Moment(
            id=moment.id,
            user_id=moment.user_id,
            title=moment.title,
            description=moment.description,
            voice_input=moment.voice_input,
            ai_summary=moment.ai_summary,
            category=moment.category,
            tags=moment.tags,
            occurred_at=moment.occurred_at,
            timezone=moment.timezone,
            location=moment.location,
            emotion=moment.emotion,
            revision=moment.revision + 1,
            created_at=moment.created_at,
            updated_at=now,
            deleted_at=now,
        )
        self._store[moment.id] = deleted
        return deleted

    @staticmethod
    def _parse_location(data: dict | None) -> MomentLocation | None:
        if not data:
            return None
        source = data.get("source", "unknown")
        return MomentLocation(
            name=data.get("name"),
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            source=LocationSource(source) if isinstance(source, str) else source,
        )

    @staticmethod
    def _parse_emotion(data: dict | None) -> MomentEmotion | None:
        if not data:
            return None
        return MomentEmotion(
            label=data.get("label", ""),
            valence=data.get("valence"),
            arousal=data.get("arousal"),
        )
