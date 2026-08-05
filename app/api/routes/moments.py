import contextlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth_context, get_authenticated_user_id
from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.audit_event_repository import (
    SqlAuditEventRepository,
)
from app.infrastructure.database.repositories.confirmation_repository import (
    SqlConfirmationRepository,
)
from app.infrastructure.database.repositories.idempotency_repository import (
    SqlIdempotencyRepository,
    fingerprint_payload,
)
from app.infrastructure.database.repositories.moment_repository import (
    PostgresMomentRepository,
)
from app.infrastructure.database.repositories.moment_revision_repository import (
    SqlMomentRevisionRepository,
)
from app.infrastructure.database.session import get_db_session
from app.modules.moments.domain import (
    LocationSource,
    Moment,
    MomentCategory,
    MomentEmotion,
    MomentLocation,
    MomentProvenance,
    ProvenanceSource,
)

router = APIRouter(prefix="/v1/moments", tags=["moments"])


async def _get_user_id(
    user_id: UUID = Depends(get_authenticated_user_id),
) -> UUID:
    """鉴权依赖：支持 Casdoor OIDC 和眼镜端 JWT 双通道。"""
    return user_id


def _infer_provenance(ctx: AuthContext, body_provenance: dict | None) -> MomentProvenance:
    """从 AuthContext 推断 provenance，客户端显式传入优先。

    不可篡改：仅创建时生效，update 路由不接受 provenance。
    """
    if body_provenance:
        return MomentProvenance.from_dict(body_provenance) or MomentProvenance(
            source=ProvenanceSource.WEB
        )
    # 服务端推断
    if ctx.method == "glasses":
        return MomentProvenance(
            source=ProvenanceSource.ROKID,
            device_id=ctx.device_id,
        )
    return MomentProvenance(source=ProvenanceSource.WEB)


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
        "provenance": moment.provenance.to_dict() if moment.provenance else None,
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
    provenance: dict | None = None  # 客户端可显式声明，否则服务端按 AuthContext 推断


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
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    # 幂等去重：客户端传 Idempotency-Key 时启用
    idem_record = None
    idem_repo: SqlIdempotencyRepository | None = None
    if idempotency_key:
        idem_repo = SqlIdempotencyRepository(session)
        request_fp = fingerprint_payload(body.model_dump())
        idem_record = await idem_repo.acquire(
            user_id=ctx.user_id,
            operation="create_moment",
            idempotency_key=idempotency_key,
            request_payload=body.model_dump(),
        )
        # fingerprint 不一致 → 冲突（无论 state）
        if idem_record.request_fingerprint != request_fp:
            raise ApplicationError(
                code="IDEMPOTENCY_CONFLICT",
                message="Idempotency-Key 已用于不同的请求体。",
                status_code=409,
            )
        # 命中缓存：返回原响应
        if idem_record.state == "completed" and idem_record.response_body is not None:
            return idem_record.response_body

    occurred_at = datetime.now(UTC)
    if body.occurredAt:
        occurred_at = datetime.fromisoformat(body.occurredAt.replace("Z", "+00:00"))

    moment = Moment(
        id=uuid4(),
        user_id=ctx.user_id,
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
        provenance=_infer_provenance(ctx, body.provenance),
    )

    repo = PostgresMomentRepository(session)
    created = await repo.create(moment)
    response = _to_dict(created)

    # 记录版本快照
    revision_repo = SqlMomentRevisionRepository(session)
    await revision_repo.append(
        user_id=created.user_id,
        moment_id=created.id,
        revision=created.revision,
        operation="created",
        snapshot=response,
        actor_user_id=ctx.user_id,
    )

    # 审计
    audit_repo = SqlAuditEventRepository(session)
    await audit_repo.append(
        user_id=ctx.user_id,
        actor_type="web" if ctx.method == "casdoor" else "device",
        actor_id=str(ctx.device_id) if ctx.method == "glasses" else None,
        event_type="moment.created",
        resource_type="moment",
        resource_id=created.id,
        allowed=True,
    )

    # 写入幂等缓存
    if idem_repo is not None and idem_record is not None:
        await idem_repo.complete(
            record_id=idem_record.id,
            response_status=status.HTTP_201_CREATED,
            response_body=response,
            resource_id=created.id,
        )

    return response


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
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    user_id = ctx.user_id
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
    response = _to_dict(moment)

    # 记录版本快照
    revision_repo = SqlMomentRevisionRepository(session)
    await revision_repo.append(
        user_id=user_id,
        moment_id=moment.id,
        revision=moment.revision,
        operation="updated",
        snapshot=response,
        actor_user_id=ctx.user_id,
    )

    # 审计
    audit_repo = SqlAuditEventRepository(session)
    await audit_repo.append(
        user_id=user_id,
        actor_type="web" if ctx.method == "casdoor" else "device",
        actor_id=str(ctx.device_id) if ctx.method == "glasses" else None,
        event_type="moment.updated",
        resource_type="moment",
        resource_id=moment.id,
        allowed=True,
    )

    return response


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

    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)
    preview = {
        "title": moment.title,
        "category": moment.category.value,
        "occurredAt": moment.occurred_at.isoformat(),
    }

    confirmation_repo = SqlConfirmationRepository(session)
    confirmation = await confirmation_repo.create(
        user_id=user_id,
        target_type="moment",
        target_id=moment.id,
        action="delete",
        expected_revision=moment.revision,
        preview=preview,
        expires_at=expires_at,
    )

    return {
        "confirmationId": str(confirmation.id),
        "expiresAt": confirmation.expires_at.isoformat(),
        "revision": moment.revision,
    }


@router.post("/delete-confirm", status_code=status.HTTP_204_NO_CONTENT)
async def delete_confirm(
    body: DeleteConfirmRequest,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    confirmation_repo = SqlConfirmationRepository(session)
    confirmation = await confirmation_repo.get(UUID(body.confirmationId))
    if confirmation is None:
        raise ApplicationError(
            code="CONFIRMATION_REQUIRED",
            message="请先执行删除预览。",
            status_code=400,
        )
    if confirmation.user_id != user_id:
        # 不泄露存在性，统一返回 CONFIRMATION_REQUIRED
        raise ApplicationError(
            code="CONFIRMATION_REQUIRED",
            message="请先执行删除预览。",
            status_code=400,
        )
    if confirmation.status == "used":
        raise ApplicationError(
            code="CONFIRMATION_USED",
            message="该确认已使用，请重新发起删除。",
            status_code=400,
        )
    if datetime.now(UTC) > confirmation.expires_at:
        raise ApplicationError(
            code="CONFIRMATION_EXPIRED",
            message="确认已过期，请重新发起删除。",
            status_code=400,
        )

    # 同一事务内：消费票据 + 软删除 Moment + 记录版本 + 审计
    await confirmation_repo.mark_used(confirmation_id=confirmation.id, used_at=datetime.now(UTC))
    repo = PostgresMomentRepository(session)
    deleted = await repo.soft_delete(confirmation.target_id, user_id)
    if deleted is not None:
        revision_repo = SqlMomentRevisionRepository(session)
        await revision_repo.append(
            user_id=user_id,
            moment_id=deleted.id,
            revision=deleted.revision,
            operation="deleted",
            snapshot=_to_dict(deleted),
            actor_user_id=user_id,
        )
    audit_repo = SqlAuditEventRepository(session)
    await audit_repo.append(
        user_id=user_id,
        actor_type="web",
        actor_id=str(user_id),
        event_type="moment.deleted",
        resource_type="moment",
        resource_id=confirmation.target_id,
        allowed=True,
    )
