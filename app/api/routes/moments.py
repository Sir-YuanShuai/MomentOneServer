import contextlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.moment_repository import (
    PostgresMomentRepository,
)
from app.infrastructure.database.repositories.user_repository import resolve_user_id
from app.infrastructure.database.session import get_db_session
from app.infrastructure.identity.casdoor import CasdoorTokenVerifier
from app.modules.moments.domain import (
    LocationSource,
    Moment,
    MomentCategory,
    MomentEmotion,
    MomentLocation,
)

router = APIRouter(prefix="/v1/moments", tags=["moments"])

_confirmations: dict[str, dict] = {}


def _get_verifier(settings: Settings = Depends(get_settings)) -> CasdoorTokenVerifier:
    return CasdoorTokenVerifier(settings)


async def _get_user_id(
    settings: Settings = Depends(get_settings),
    verifier: CasdoorTokenVerifier = Depends(_get_verifier),
    session: AsyncSession = Depends(get_db_session),
    authorization: str | None = Header(default=None),
) -> UUID:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApplicationError(
            code="AUTH_REQUIRED",
            message="请先登录。",
            status_code=401,
        )

    token = authorization.removeprefix("Bearer ").strip()
    return await resolve_user_id(session, verifier, token)


def _parse_location(data: dict | None) -> MomentLocation | None:
    if not data:
        return None
    source = LocationSource.UNKNOWN
    if data.get("source"):
        with contextlib.suppress(ValueError):
            source = LocationSource(data["source"])
    return MomentLocation(
        name=data.get("name"),
        latitude=data.get("latitude"),
        longitude=data.get("longitude"),
        source=source,
    )


def _parse_emotion(data: dict | None) -> MomentEmotion | None:
    if not data:
        return None
    return MomentEmotion(
        label=data.get("label", ""),
        valence=data.get("valence"),
        arousal=data.get("arousal"),
    )


def _to_dict(moment: Moment) -> dict:
    return {
        "id": str(moment.id),
        "userId": str(moment.user_id),
        "title": moment.title,
        "description": moment.description,
        "voiceInput": moment.voice_input,
        "aiSummary": moment.ai_summary,
        "category": moment.category.value,
        "tags": list(moment.tags),
        "occurredAt": moment.occurred_at.isoformat(),
        "timezone": moment.timezone,
        "location": (
            {
                "name": moment.location.name,
                "latitude": moment.location.latitude,
                "longitude": moment.location.longitude,
                "source": moment.location.source.value,
            }
            if moment.location
            else None
        ),
        "emotion": (
            {
                "label": moment.emotion.label,
                "valence": moment.emotion.valence,
                "arousal": moment.emotion.arousal,
            }
            if moment.emotion
            else None
        ),
        "revision": moment.revision,
        "createdAt": moment.created_at.isoformat(),
        "updatedAt": moment.updated_at.isoformat(),
        "deletedAt": moment.deleted_at.isoformat() if moment.deleted_at else None,
    }


class CreateMomentRequest(BaseModel):
    title: str = Field(max_length=20)
    description: str | None = Field(default=None, max_length=240)
    voiceInput: str | None = None
    aiSummary: str | None = Field(default=None, max_length=80)
    category: str = "experience"
    tags: list[str] = Field(default_factory=list, max_length=5)
    occurredAt: str | None = None
    timezone: str = "UTC"
    location: dict | None = None
    emotion: dict | None = None


class UpdateMomentRequest(BaseModel):
    expectedRevision: int
    title: str | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None, max_length=240)
    aiSummary: str | None = Field(default=None, max_length=80)
    category: str | None = None
    tags: list[str] | None = Field(default=None, max_length=5)
    occurredAt: str | None = None
    timezone: str | None = None
    location: dict | None = None
    emotion: dict | None = None


class DeletePreviewRequest(BaseModel):
    expectedRevision: int


class DeleteConfirmRequest(BaseModel):
    confirmationId: str


class CursorPageResponse(BaseModel):
    items: list[dict]
    nextCursor: str | None = None
    hasMore: bool = False


@router.get("", response_model=CursorPageResponse)
async def list_moments(
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> CursorPageResponse:
    repo = PostgresMomentRepository(session)
    moments, has_more, next_cursor = await repo.list_by_user(
        user_id=user_id,
        limit=limit,
        cursor=cursor,
    )
    return CursorPageResponse(
        items=[_to_dict(m) for m in moments],
        nextCursor=next_cursor,
        hasMore=has_more,
    )


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_moment(
    body: CreateMomentRequest,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    occurred_at = datetime.now(UTC)
    if body.occurredAt:
        occurred_at = datetime.fromisoformat(body.occurredAt.replace("Z", "+00:00"))

    moment = Moment(
        id=uuid4(),
        user_id=user_id,
        title=body.title,
        description=body.description,
        voice_input=body.voiceInput,
        ai_summary=body.aiSummary,
        category=MomentCategory(body.category),
        tags=tuple(body.tags),
        occurred_at=occurred_at,
        timezone=body.timezone,
        revision=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        location=_parse_location(body.location),
        emotion=_parse_emotion(body.emotion),
    )

    repo = PostgresMomentRepository(session)
    created = await repo.create(moment)
    return _to_dict(created)


@router.get("/{moment_id}", response_model=dict)
async def get_moment(
    moment_id: str,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    repo = PostgresMomentRepository(session)
    moment = await repo.get_by_id(UUID(moment_id), user_id)
    if moment is None:
        raise ApplicationError(
            code="MOMENT_NOT_FOUND",
            message="未找到该 Moment。",
            status_code=404,
        )
    return _to_dict(moment)


@router.patch("/{moment_id}", response_model=dict)
async def update_moment(
    moment_id: str,
    body: UpdateMomentRequest,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    repo = PostgresMomentRepository(session)
    existing = await repo.get_by_id(UUID(moment_id), user_id)
    if existing is None:
        raise ApplicationError(
            code="MOMENT_NOT_FOUND",
            message="未找到该 Moment。",
            status_code=404,
        )

    if existing.revision != body.expectedRevision:
        raise ApplicationError(
            code="REVISION_CONFLICT",
            message="Moment 已被其他操作修改，请刷新后重试。",
            status_code=409,
            details={
                "expectedRevision": body.expectedRevision,
                "actualRevision": existing.revision,
            },
        )

    fields = {}
    if body.title is not None:
        fields["title"] = body.title
    if body.description is not None:
        fields["description"] = body.description
    if body.aiSummary is not None:
        fields["ai_summary"] = body.aiSummary
    if body.category is not None:
        fields["category"] = MomentCategory(body.category)
    if body.tags is not None:
        fields["tags"] = tuple(body.tags)
    if body.occurredAt is not None:
        fields["occurred_at"] = datetime.fromisoformat(body.occurredAt.replace("Z", "+00:00"))
    if body.location is not None:
        fields["location"] = _parse_location(body.location)
    if body.emotion is not None:
        fields["emotion"] = _parse_emotion(body.emotion)

    moment = await repo.update(UUID(moment_id), user_id, **fields)
    if moment is None:
        raise ApplicationError(
            code="MOMENT_NOT_FOUND",
            message="未找到该 Moment。",
            status_code=404,
        )
    return _to_dict(moment)


@router.post("/{moment_id}/delete-preview", response_model=dict)
async def delete_preview(
    moment_id: str,
    body: DeletePreviewRequest,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    repo = PostgresMomentRepository(session)
    moment = await repo.get_by_id(UUID(moment_id), user_id)
    if moment is None:
        raise ApplicationError(
            code="MOMENT_NOT_FOUND",
            message="未找到该 Moment。",
            status_code=404,
        )

    if moment.revision != body.expectedRevision:
        raise ApplicationError(
            code="REVISION_CONFLICT",
            message="Moment 已被其他操作修改，请刷新后重试。",
            status_code=409,
            details={
                "expectedRevision": body.expectedRevision,
                "actualRevision": moment.revision,
            },
        )

    confirmation_id = str(uuid4())
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)

    _confirmations[confirmation_id] = {
        "moment_id": moment.id,
        "user_id": user_id,
        "expires_at": expires_at,
        "used": False,
    }

    return {
        "confirmationId": confirmation_id,
        "expiresAt": expires_at.isoformat(),
        "revision": moment.revision,
    }


@router.post("/delete-confirm", status_code=status.HTTP_204_NO_CONTENT)
async def delete_confirm(
    body: DeleteConfirmRequest,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    record = _confirmations.get(body.confirmationId)
    if record is None:
        raise ApplicationError(
            code="CONFIRMATION_REQUIRED",
            message="请先执行删除预览。",
            status_code=400,
        )
    if record["used"]:
        raise ApplicationError(
            code="CONFIRMATION_USED",
            message="该确认已使用，请重新发起删除。",
            status_code=400,
        )
    if datetime.now(UTC) > record["expires_at"]:
        raise ApplicationError(
            code="CONFIRMATION_EXPIRED",
            message="确认已过期，请重新发起删除。",
            status_code=400,
        )

    record["used"] = True
    repo = PostgresMomentRepository(session)
    await repo.soft_delete(record["moment_id"], user_id)
