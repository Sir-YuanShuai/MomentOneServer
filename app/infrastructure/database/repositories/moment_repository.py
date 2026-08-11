from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import Moment as MomentORM
from app.modules.moments.domain import (
    LocationSource,
    Moment,
    MomentCategory,
    MomentEmotion,
    MomentLocation,
    MomentProvenance,
)


def _orm_to_domain(orm: MomentORM) -> Moment:
    location = None
    if orm.location_name or orm.location_latitude or orm.location_longitude:
        location = MomentLocation(
            name=orm.location_name,
            latitude=orm.location_latitude,
            longitude=orm.location_longitude,
            source=LocationSource(orm.location_source)
            if orm.location_source
            else LocationSource.UNKNOWN,
        )

    emotion = None
    if orm.emotion_label:
        emotion = MomentEmotion(
            label=orm.emotion_label,
            valence=orm.emotion_score,
        )

    provenance = MomentProvenance.from_dict(orm.provenance) if orm.provenance else None

    return Moment(
        id=orm.id,
        user_id=orm.user_id,
        title=orm.title,
        description=orm.description,
        voice_input=orm.voice_input,
        ai_summary=orm.ai_summary,
        category=MomentCategory(orm.category),
        tags=tuple(orm.tags or []),
        persons=tuple(orm.persons or []),
        event=orm.event_name,
        occurred_at=orm.occurred_at,
        timezone=orm.timezone,
        revision=orm.revision,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
        location=location,
        emotion=emotion,
        provenance=provenance,
        deleted_at=orm.deleted_at,
        moment_type=orm.moment_type or "general",
        payload=orm.payload or {},
    )


class PostgresMomentRepository:
    """PostgreSQL-backed Moment repository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, moment: Moment) -> Moment:
        orm = MomentORM(
            id=moment.id if moment.id else uuid4(),
            user_id=moment.user_id,
            title=moment.title,
            description=moment.description,
            voice_input=moment.voice_input,
            ai_summary=moment.ai_summary,
            category=moment.category.value,
            tags=list(moment.tags),
            persons=list(moment.persons),
            event_name=moment.event,
            occurred_at=moment.occurred_at,
            timezone=moment.timezone,
            location_name=moment.location.name if moment.location else None,
            location_latitude=moment.location.latitude if moment.location else None,
            location_longitude=moment.location.longitude if moment.location else None,
            location_source=moment.location.source.value if moment.location else None,
            emotion_label=moment.emotion.label if moment.emotion else None,
            emotion_score=moment.emotion.valence if moment.emotion else None,
            provenance=moment.provenance.to_dict() if moment.provenance else None,
            revision=moment.revision,
            moment_type=moment.moment_type or "general",
            payload=moment.payload or {},
        )
        self._session.add(orm)
        await self._session.flush()
        return _orm_to_domain(orm)

    async def list_all_by_type(self, user_id: UUID, moment_type: str) -> list[Moment]:
        result = await self._session.execute(
            select(MomentORM)
            .where(
                MomentORM.user_id == user_id,
                MomentORM.moment_type == moment_type,
                MomentORM.deleted_at.is_(None),
            )
            .order_by(MomentORM.occurred_at.desc(), MomentORM.id.desc())
        )
        return [_orm_to_domain(item) for item in result.scalars().all()]

    async def list_by_type_and_time(
        self,
        user_id: UUID,
        moment_type: str,
        *,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
    ) -> list[Moment]:
        stmt = select(MomentORM).where(
            MomentORM.user_id == user_id,
            MomentORM.moment_type == moment_type,
            MomentORM.deleted_at.is_(None),
        )
        if occurred_from is not None:
            stmt = stmt.where(MomentORM.occurred_at >= occurred_from)
        if occurred_to is not None:
            stmt = stmt.where(MomentORM.occurred_at <= occurred_to)
        result = await self._session.execute(
            stmt.order_by(MomentORM.occurred_at.asc(), MomentORM.id.asc())
        )
        return [_orm_to_domain(item) for item in result.scalars().all()]

    async def count_by_type(self, user_id: UUID, moment_type: str) -> int:
        result = await self._session.scalar(
            select(func.count())
            .select_from(MomentORM)
            .where(
                MomentORM.user_id == user_id,
                MomentORM.moment_type == moment_type,
                MomentORM.deleted_at.is_(None),
            )
        )
        return int(result or 0)

    async def count_all(self, user_id: UUID) -> int:
        result = await self._session.scalar(
            select(func.count())
            .select_from(MomentORM)
            .where(MomentORM.user_id == user_id, MomentORM.deleted_at.is_(None))
        )
        return int(result or 0)

    async def list_all(self, user_id: UUID) -> list[Moment]:
        result = await self._session.execute(
            select(MomentORM)
            .where(MomentORM.user_id == user_id, MomentORM.deleted_at.is_(None))
            .order_by(MomentORM.occurred_at.desc(), MomentORM.id.desc())
        )
        return [_orm_to_domain(item) for item in result.scalars().all()]

    async def soft_delete_all_by_type(self, user_id: UUID, moment_type: str) -> int:
        result = await self._session.execute(
            update(MomentORM)
            .where(
                MomentORM.user_id == user_id,
                MomentORM.moment_type == moment_type,
                MomentORM.deleted_at.is_(None),
            )
            .values(deleted_at=datetime.now(UTC), revision=MomentORM.revision + 1)
        )
        return int(cast(CursorResult, result).rowcount or 0)

    async def soft_delete_all(self, user_id: UUID) -> int:
        result = await self._session.execute(
            update(MomentORM)
            .where(MomentORM.user_id == user_id, MomentORM.deleted_at.is_(None))
            .values(deleted_at=datetime.now(UTC), revision=MomentORM.revision + 1)
        )
        return int(cast(CursorResult, result).rowcount or 0)

    async def list_by_user(
        self,
        user_id: UUID,
        *,
        limit: int = 20,
        cursor: str | None = None,
        moment_type: str | None = None,
        category: str | None = None,
        tag: str | None = None,
        goal_id: UUID | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        payload_eq: dict[str, str] | None = None,
    ) -> tuple[list[Moment], bool, str | None]:
        stmt = (
            select(MomentORM)
            .where(
                and_(
                    MomentORM.user_id == user_id,
                    MomentORM.deleted_at.is_(None),
                )
            )
            .order_by(MomentORM.occurred_at.desc(), MomentORM.id.desc())
            .limit(limit + 1)
        )

        if moment_type:
            stmt = stmt.where(MomentORM.moment_type == moment_type)
        if category:
            stmt = stmt.where(MomentORM.category == category)
        if tag:
            stmt = stmt.where(MomentORM.tags.contains([tag]))
        if goal_id:
            stmt = stmt.where(MomentORM.payload["goalId"].astext == str(goal_id))
        if occurred_from:
            stmt = stmt.where(MomentORM.occurred_at >= occurred_from)
        if occurred_to:
            stmt = stmt.where(MomentORM.occurred_at <= occurred_to)
        if payload_eq:
            for key, value in payload_eq.items():
                stmt = stmt.where(MomentORM.payload[key].astext == value)

        if cursor:
            # cursor 是上一页最后一条的 occurred_at + id 的组合
            parts = cursor.split("|")
            if len(parts) == 2:
                occurred_at_str, moment_id = parts
                try:
                    cursor_time = datetime.fromisoformat(occurred_at_str)
                    stmt = stmt.where(
                        or_(
                            MomentORM.occurred_at < cursor_time,
                            and_(
                                MomentORM.occurred_at == cursor_time,
                                MomentORM.id < UUID(moment_id),
                            ),
                        )
                    )
                except (ValueError, TypeError):
                    pass

        result = await self._session.execute(stmt)
        rows = result.scalars().all()

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        moments = [_orm_to_domain(r) for r in rows]
        next_cursor = None
        if has_more and moments:
            last = moments[-1]
            next_cursor = f"{last.occurred_at.isoformat()}|{last.id}"

        return moments, has_more, next_cursor

    async def list_by_user_range(
        self,
        user_id: UUID,
        *,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        moment_type: str | None = None,
        payload_eq: dict[str, str] | None = None,
    ) -> list[Moment]:
        """区间全量查询（无分页），供服务端聚合（如 bookkeeping_summary）。

        仅用于个人数据量级（单用户周期内账单数有限）；输出仍受工具层限制。
        """
        stmt = (
            select(MomentORM)
            .where(
                and_(
                    MomentORM.user_id == user_id,
                    MomentORM.deleted_at.is_(None),
                )
            )
            .order_by(MomentORM.occurred_at.desc(), MomentORM.id.desc())
        )
        if moment_type:
            stmt = stmt.where(MomentORM.moment_type == moment_type)
        if occurred_from:
            stmt = stmt.where(MomentORM.occurred_at >= occurred_from)
        if occurred_to:
            stmt = stmt.where(MomentORM.occurred_at <= occurred_to)
        if payload_eq:
            for key, value in payload_eq.items():
                stmt = stmt.where(MomentORM.payload[key].astext == value)
        result = await self._session.execute(stmt)
        return [_orm_to_domain(r) for r in result.scalars().all()]

    async def get_by_id(self, moment_id: UUID, user_id: UUID) -> Moment | None:
        stmt = select(MomentORM).where(
            and_(
                MomentORM.id == moment_id,
                MomentORM.user_id == user_id,
                MomentORM.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        return _orm_to_domain(orm) if orm else None

    async def update(self, moment_id: UUID, user_id: UUID, **fields) -> Moment | None:
        stmt = select(MomentORM).where(
            and_(
                MomentORM.id == moment_id,
                MomentORM.user_id == user_id,
                MomentORM.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if not orm:
            return None

        if "title" in fields:
            orm.title = fields["title"]
        if "description" in fields:
            orm.description = fields["description"]
        if "ai_summary" in fields:
            orm.ai_summary = fields["ai_summary"]
        if "category" in fields:
            orm.category = (
                fields["category"].value
                if hasattr(fields["category"], "value")
                else fields["category"]
            )
        if "tags" in fields:
            orm.tags = list(fields["tags"])
        if "persons" in fields:
            orm.persons = list(fields["persons"])
        if "event" in fields:
            orm.event_name = fields["event"]
        if "occurred_at" in fields:
            orm.occurred_at = fields["occurred_at"]
        if "location" in fields:
            loc = fields["location"]
            if loc:
                orm.location_name = loc.name
                orm.location_latitude = loc.latitude
                orm.location_longitude = loc.longitude
                orm.location_source = loc.source.value
            else:
                orm.location_name = None
                orm.location_latitude = None
                orm.location_longitude = None
                orm.location_source = None
        if "emotion" in fields:
            emo = fields["emotion"]
            if emo:
                orm.emotion_label = emo.label
                orm.emotion_score = emo.valence
            else:
                orm.emotion_label = None
                orm.emotion_score = None
        if "moment_type" in fields:
            orm.moment_type = fields["moment_type"]
        if "payload" in fields:
            orm.payload = fields["payload"] or {}

        orm.revision += 1
        await self._session.flush()
        await self._session.refresh(orm)
        return _orm_to_domain(orm)

    async def soft_delete(self, moment_id: UUID, user_id: UUID) -> Moment | None:
        stmt = select(MomentORM).where(
            and_(
                MomentORM.id == moment_id,
                MomentORM.user_id == user_id,
                MomentORM.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        if not orm:
            return None
        orm.deleted_at = datetime.now(UTC)
        orm.revision = orm.revision + 1
        await self._session.flush()
        await self._session.refresh(orm)
        return _orm_to_domain(orm)
