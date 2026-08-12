import contextlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth_context, get_authenticated_user_id
from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.asset_repository import (
    AssetRepository,
    MomentAssetRepository,
)
from app.infrastructure.database.repositories.audit_event_repository import (
    SqlAuditEventRepository,
)
from app.infrastructure.database.repositories.confirmation_repository import (
    SqlConfirmationRepository,
)
from app.infrastructure.database.repositories.habit_goal_repository import (
    SqlHabitGoalRepository,
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
from app.infrastructure.storage.object_storage import (
    ObjectStorage,
    ObjectStorageNotConfigured,
    get_object_storage,
)
from app.modules.assets.domain import AssetRole, AssetState
from app.modules.moment_types.registry import validate as validate_moment_type
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
    if ctx.method == "mcp":
        return MomentProvenance(
            source=ProvenanceSource.MCP,
            client_id=ctx.client_id,
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


def _actor_type(ctx: AuthContext) -> str:
    """审计 actorType：casdoor→web / glasses→device / mcp→mcp。"""
    if ctx.method == "glasses":
        return "device"
    if ctx.method == "mcp":
        return "mcp"
    return "web"


async def _validate_habit_goal_ref(
    payload: dict | None,
    user_id: UUID,
    session: AsyncSession,
) -> None:
    """habit 打卡记录若带 payload.goalId，必须存在且归属当前用户。"""
    if not payload or not payload.get("goalId"):
        return
    try:
        goal_id = UUID(payload["goalId"])
    except (ValueError, TypeError) as exc:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="payload.goalId 不是合法的 UUID。",
            status_code=400,
        ) from exc
    repo = SqlHabitGoalRepository(session)
    goal = await repo.get_by_id(goal_id, user_id)
    if goal is None:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="payload.goalId 对应的习惯目标不存在或不属于当前用户。",
            status_code=400,
            details={"goalId": str(goal_id)},
        )


def get_storage(
    settings: Settings = Depends(get_settings),
) -> ObjectStorage | None:
    """对象存储依赖；未配置时返回 None（media 字段省略 downloadUrl/thumbnailUrl）。"""
    try:
        return get_object_storage(settings)
    except ObjectStorageNotConfigured:
        return None


async def _build_media(
    moment_id: UUID,
    user_id: UUID,
    session: AsyncSession,
    storage: ObjectStorage | None,
    settings: Settings,
    *,
    include_download_url: bool = True,
) -> list[dict]:
    """读取 moment_assets + assets，组装 media 响应。

    - thumbnailUrl：仅当缩略图已生成（thumbnail_generated_at 非空）时签发；
      存量文件/非 image 类保持 null，前端降级为图标占位（不加载图片，避免慢）。
    - 列表场景：include_download_url=False，只返回 thumbnailUrl
    - 详情场景：include_download_url=True，返回 downloadUrl + thumbnailUrl
    - storage 未配置时省略所有 URL 字段
    """
    link_repo = MomentAssetRepository(session)
    asset_repo = AssetRepository(session)
    links = await link_repo.list_by_moment(moment_id, user_id)

    media: list[dict] = []
    for link in links:
        asset = await asset_repo.get_by_id(link.asset_id, user_id)
        if asset is None or asset.state != AssetState.READY:
            continue
        entry: dict = {
            "assetId": str(asset.id),
            "type": asset.content_type,
            "thumbnailUrl": None,  # 缩略图已生成才签发，存量/非 image 保持 null（前端降级）
        }
        if storage is not None and asset.thumbnail_generated_at is not None:
            entry["thumbnailUrl"] = storage.create_thumbnail_url(
                user_id=str(user_id),
                asset_id=str(asset.id),
                expires_in_seconds=settings.s3_download_url_ttl_seconds,
            )
        if include_download_url and storage is not None:
            url = storage.create_download_url(
                user_id=str(user_id),
                asset_id=str(asset.id),
                expires_in_seconds=settings.s3_download_url_ttl_seconds,
            )
            expires_at = datetime.now(UTC) + timedelta(seconds=settings.s3_download_url_ttl_seconds)
            entry["downloadUrl"] = url
            entry["expiresAt"] = expires_at.isoformat()
        media.append(entry)
    return media


def _revision_media_snapshot(media: list[dict]) -> list[dict]:
    """只保留稳定媒体引用，避免把短期签名 URL 写入版本快照。"""
    return [
        {
            "assetId": item["assetId"],
            "type": item["type"],
            "thumbnailUrl": None,
        }
        for item in media
    ]


def _to_dict(moment: Moment, media: list[dict] | None = None) -> dict:
    d: dict = {
        "id": str(moment.id),
        "userId": str(moment.user_id),
        "title": moment.title,
        "description": moment.description,
        "voiceInput": moment.voice_input,
        "aiSummary": moment.ai_summary,
        "category": moment.category.value,
        "type": moment.moment_type,
        "payload": moment.payload,
        "tags": list(moment.tags),
        "persons": list(moment.persons),
        "event": moment.event,
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
    if media is not None:
        d["media"] = media
    return d


class CreateMomentRequest(BaseModel):
    id: UUID | None = Field(
        default=None,
        description="客户端离线优先创建时生成的 UUID；省略则由服务端生成。",
    )
    title: str = Field(max_length=20)
    description: str | None = Field(default=None, max_length=240)
    voiceInput: str | None = None
    aiSummary: str | None = Field(default=None, max_length=80)
    category: str = "experience"
    tags: list[str] = Field(default_factory=list, max_length=5)
    persons: list[str] = Field(default_factory=list, max_length=10)
    event: str | None = Field(default=None, max_length=50)
    occurredAt: str | None = None
    timezone: str = "UTC"
    location: dict | None = None
    emotion: dict | None = None
    provenance: dict | None = None  # 客户端可显式声明，否则服务端按 AuthContext 推断
    type: str = "general"  # 记录类型（注册表驱动，general 兜底）
    payload: dict | None = None  # 类型化扩展字段，按类型 Schema 校验
    assetIds: list[str] = Field(
        default_factory=list, description="已上传完成的 Asset ID 列表，按顺序关联"
    )

    @field_validator("persons")
    @classmethod
    def _persons_item_length(cls, value: list[str]) -> list[str]:
        for item in value:
            if len(item) > 20:
                raise ValueError("persons 每项不能超过 20 字")
        return value


class UpdateMomentRequest(BaseModel):
    expectedRevision: int
    title: str | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None, max_length=240)
    aiSummary: str | None = Field(default=None, max_length=80)
    category: str | None = None
    tags: list[str] | None = Field(default=None, max_length=5)
    persons: list[str] | None = Field(default=None, max_length=10)
    event: str | None = Field(default=None, max_length=50)
    occurredAt: str | None = None
    timezone: str | None = None
    location: dict | None = None
    emotion: dict | None = None
    type: str | None = None  # 记录类型；None = 不修改
    payload: dict | None = None  # 类型化扩展字段；None = 不修改，{} = 清空
    assetIds: list[str] | None = Field(
        default=None,
        description="附件列表；省略表示保持不变，空数组表示移除全部附件",
    )

    @field_validator("persons")
    @classmethod
    def _persons_item_length(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for item in value:
            if len(item) > 20:
                raise ValueError("persons 每项不能超过 20 字")
        return value


class DeletePreviewRequest(BaseModel):
    expectedRevision: int


class DeleteConfirmRequest(BaseModel):
    confirmationId: str


class BatchDeleteItem(BaseModel):
    id: UUID
    expectedRevision: int = Field(ge=1)


class BatchDeletePreviewRequest(BaseModel):
    items: list[BatchDeleteItem] = Field(min_length=1, max_length=100)


class CursorPageResponse(BaseModel):
    items: list[dict]
    nextCursor: str | None = None
    hasMore: bool = False


@router.get("", response_model=CursorPageResponse)
async def list_moments(
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
    storage: ObjectStorage | None = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    type: str | None = Query(
        default=None, description="按记录类型过滤（general/bookkeeping/habit）"
    ),
    category: str | None = Query(default=None, description="按分类过滤"),
    tag: str | None = Query(default=None, description="按标签过滤"),
    goalId: str | None = Query(default=None, description="按习惯目标过滤（payload.goalId）"),
) -> CursorPageResponse:
    repo = PostgresMomentRepository(session)
    moments, has_more, next_cursor = await repo.list_by_user(
        user_id=user_id,
        limit=limit,
        cursor=cursor,
        moment_type=type,
        category=category,
        tag=tag,
        goal_id=UUID(goalId) if goalId else None,
    )
    items = []
    for m in moments:
        media = await _build_media(
            m.id, user_id, session, storage, settings, include_download_url=False
        )
        items.append(_to_dict(m, media=media))
    return CursorPageResponse(
        items=items,
        nextCursor=next_cursor,
        hasMore=has_more,
    )


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_moment(
    body: CreateMomentRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    storage: ObjectStorage | None = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    # 幂等去重：客户端传 Idempotency-Key 时启用
    idem_record = None
    idem_repo: SqlIdempotencyRepository | None = None
    if idempotency_key:
        idem_repo = SqlIdempotencyRepository(session)
        idempotency_payload = body.model_dump(mode="json")
        request_fp = fingerprint_payload(idempotency_payload)
        idem_record = await idem_repo.acquire(
            user_id=ctx.user_id,
            operation="create_moment",
            idempotency_key=idempotency_key,
            request_payload=idempotency_payload,
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

    # 记录类型校验（D2/D3）：类型存在 + payload 符合类型 Schema
    validate_moment_type(body.type, body.payload or {})
    # habit 打卡关联的习惯目标归属校验（payload.goalId）
    await _validate_habit_goal_ref(body.payload, ctx.user_id, session)

    if body.id is not None:
        existing = await PostgresMomentRepository(session).get_by_id_including_deleted(
            body.id, ctx.user_id
        )
        if existing is not None:
            raise ApplicationError(
                code="MOMENT_ID_CONFLICT",
                message="该客户端记录 ID 已存在。",
                status_code=409,
                details={"momentId": str(body.id)},
            )

    moment = Moment(
        id=body.id or uuid4(),
        user_id=ctx.user_id,
        title=body.title,
        description=body.description,
        voice_input=body.voiceInput,
        ai_summary=body.aiSummary,
        category=MomentCategory(body.category),
        tags=tuple(body.tags),
        persons=tuple(body.persons),
        event=body.event,
        occurred_at=occurred_at,
        timezone=body.timezone,
        revision=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        location=_parse_location(body.location),
        emotion=_parse_emotion(body.emotion),
        provenance=_infer_provenance(ctx, body.provenance),
        moment_type=body.type,
        payload=body.payload or {},
    )

    repo = PostgresMomentRepository(session)
    created = await repo.create(moment)

    # 关联 Asset（如有）：校验每个 assetId 属于当前用户且 state=ready
    media: list[dict] = []
    if body.assetIds:
        asset_repo = AssetRepository(session)
        link_repo = MomentAssetRepository(session)
        for position, asset_id_str in enumerate(body.assetIds):
            try:
                asset_uuid = UUID(asset_id_str)
            except (ValueError, TypeError) as exc:
                raise ApplicationError(
                    code="INVALID_ARGUMENTS",
                    message=f"assetId 格式无效：{asset_id_str}",
                    status_code=400,
                ) from exc
            asset = await asset_repo.get_by_id(asset_uuid, ctx.user_id)
            if asset is None:
                raise ApplicationError(
                    code="ASSET_NOT_FOUND",
                    message="未找到该 Asset 或无权访问。",
                    status_code=404,
                )
            if asset.state != AssetState.READY:
                raise ApplicationError(
                    code="MEDIA_NOT_READY",
                    message=f"Asset {asset_id_str} 尚未就绪，不能关联到 Moment。",
                    status_code=409,
                )
            await link_repo.attach(
                user_id=ctx.user_id,
                moment_id=created.id,
                asset_id=asset.id,
                position=position,
                role=AssetRole.ORIGINAL,
            )

    # 组装 media 响应（详情含 downloadUrl）
    media = await _build_media(
        created.id, ctx.user_id, session, storage, settings, include_download_url=True
    )
    response = _to_dict(created, media=media)

    # 记录版本快照
    revision_repo = SqlMomentRevisionRepository(session)
    await revision_repo.append(
        user_id=created.user_id,
        moment_id=created.id,
        revision=created.revision,
        operation="created",
        snapshot=_to_dict(created, media=_revision_media_snapshot(media)),
        actor_user_id=ctx.user_id,
    )

    # 审计
    audit_repo = SqlAuditEventRepository(session)
    await audit_repo.append(
        user_id=ctx.user_id,
        actor_type=_actor_type(ctx),
        actor_id=str(ctx.device_id) if ctx.method == "glasses" else ctx.client_id,
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


@router.post("/batch-delete-preview", response_model=dict)
async def batch_delete_preview(
    body: BatchDeletePreviewRequest,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    ids = [item.id for item in body.items]
    if len(ids) != len(set(ids)):
        raise ApplicationError(
            code="INVALID_ARGUMENTS", message="批量删除不能包含重复记录。", status_code=400
        )
    repo = PostgresMomentRepository(session)
    preview_items: list[dict] = []
    for item in body.items:
        moment = await repo.get_by_id(item.id, user_id)
        if moment is None:
            raise ApplicationError(
                code="MOMENT_NOT_FOUND", message="部分记录不存在或无权访问。", status_code=404
            )
        if moment.revision != item.expectedRevision:
            raise ApplicationError(
                code="REVISION_CONFLICT",
                message="部分记录已经变化，请刷新后重试。",
                status_code=409,
                details={"momentId": str(item.id), "actualRevision": moment.revision},
            )
        preview_items.append(
            {"id": str(moment.id), "revision": moment.revision, "title": moment.title}
        )
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)
    confirmation = await SqlConfirmationRepository(session).create(
        user_id=user_id,
        target_type="moments",
        target_id=uuid4(),
        action="batch_delete",
        expected_revision=len(preview_items),
        preview={"items": preview_items},
        expires_at=expires_at,
    )
    return {
        "confirmationId": str(confirmation.id),
        "expiresAt": expires_at.isoformat(),
        "count": len(preview_items),
        "items": preview_items,
    }


@router.post("/batch-delete-confirm", response_model=dict)
async def batch_delete_confirm(
    body: DeleteConfirmRequest,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    confirmations = SqlConfirmationRepository(session)
    confirmation = await confirmations.get(UUID(body.confirmationId))
    if (
        confirmation is None
        or confirmation.user_id != user_id
        or confirmation.target_type != "moments"
        or confirmation.action != "batch_delete"
    ):
        raise ApplicationError(
            code="CONFIRMATION_REQUIRED", message="请先执行批量删除预览。", status_code=400
        )
    if confirmation.status != "pending" or datetime.now(UTC) > confirmation.expires_at:
        raise ApplicationError(
            code="CONFIRMATION_EXPIRED", message="确认已失效，请重新预览。", status_code=400
        )
    raw_items = confirmation.preview.get("items")
    if not isinstance(raw_items, list):
        raise ApplicationError(
            code="CONFIRMATION_REQUIRED", message="批量删除预览无效。", status_code=400
        )
    repo = PostgresMomentRepository(session)
    resolved: list[Moment] = []
    for raw in raw_items:
        moment = await repo.get_by_id(UUID(str(raw["id"])), user_id)
        if moment is None or moment.revision != int(raw["revision"]):
            raise ApplicationError(
                code="REVISION_CONFLICT",
                message="部分记录已经变化，请重新预览。",
                status_code=409,
            )
        resolved.append(moment)
    now = datetime.now(UTC)
    await confirmations.mark_used(confirmation_id=confirmation.id, used_at=now)
    revision_repo = SqlMomentRevisionRepository(session)
    for moment in resolved:
        deleted = await repo.soft_delete(moment.id, user_id)
        if deleted is not None:
            await revision_repo.append(
                user_id=user_id,
                moment_id=deleted.id,
                revision=deleted.revision,
                operation="deleted",
                snapshot=_to_dict(deleted),
                actor_user_id=user_id,
            )
    await SqlAuditEventRepository(session).append(
        user_id=user_id,
        actor_type="web",
        actor_id=str(user_id),
        event_type="moments.batch_deleted",
        resource_type="moment",
        allowed=True,
        metadata={"count": len(resolved)},
    )
    return {"deletedCount": len(resolved)}


@router.get("/{moment_id}", response_model=dict)
async def get_moment(
    moment_id: str,
    user_id: UUID = Depends(_get_user_id),
    session: AsyncSession = Depends(get_db_session),
    storage: ObjectStorage | None = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict:
    repo = PostgresMomentRepository(session)
    moment = await repo.get_by_id(UUID(moment_id), user_id)
    if moment is None:
        raise ApplicationError(
            code="MOMENT_NOT_FOUND",
            message="未找到该 Moment。",
            status_code=404,
        )
    media = await _build_media(
        moment.id, user_id, session, storage, settings, include_download_url=True
    )
    return _to_dict(moment, media=media)


@router.patch("/{moment_id}", response_model=dict)
async def update_moment(
    moment_id: str,
    body: UpdateMomentRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    storage: ObjectStorage | None = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    user_id = ctx.user_id
    idem_record = None
    idem_repo: SqlIdempotencyRepository | None = None
    if idempotency_key:
        idem_repo = SqlIdempotencyRepository(session)
        idempotency_payload = {
            "momentId": moment_id,
            "body": body.model_dump(mode="json"),
        }
        request_fp = fingerprint_payload(idempotency_payload)
        idem_record = await idem_repo.acquire(
            user_id=user_id,
            operation="update_moment",
            idempotency_key=idempotency_key,
            request_payload=idempotency_payload,
        )
        if idem_record.request_fingerprint != request_fp:
            raise ApplicationError(
                code="IDEMPOTENCY_CONFLICT",
                message="Idempotency-Key 已用于不同的请求体。",
                status_code=409,
            )
        if idem_record.state == "completed" and idem_record.response_body is not None:
            return idem_record.response_body

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
    if body.persons is not None:
        fields["persons"] = tuple(body.persons)
    if body.event is not None:
        fields["event"] = body.event
    if body.occurredAt is not None:
        fields["occurred_at"] = datetime.fromisoformat(body.occurredAt.replace("Z", "+00:00"))
    if body.location is not None:
        fields["location"] = _parse_location(body.location)
    if body.emotion is not None:
        fields["emotion"] = _parse_emotion(body.emotion)

    # 类型变更时校验：合并现有值与本次修改，按注册表校验（D2/D3）
    if body.type is not None or body.payload is not None:
        merged_type = body.type or existing.moment_type
        merged_payload = body.payload if body.payload is not None else existing.payload
        validate_moment_type(merged_type, merged_payload)
        await _validate_habit_goal_ref(merged_payload, user_id, session)
        fields["moment_type"] = merged_type
        fields["payload"] = merged_payload

    # 附件更新采用“完整列表替换”语义：省略保持不变，[] 移除全部。
    # 先完成所有校验，再修改 Moment 和关联，避免部分更新。
    resolved_assets = None
    if body.assetIds is not None:
        if len(body.assetIds) != len(set(body.assetIds)):
            raise ApplicationError(
                code="INVALID_ARGUMENTS",
                message="assetIds 不能包含重复项。",
                status_code=400,
            )
        asset_repo = AssetRepository(session)
        resolved_assets = []
        for asset_id_str in body.assetIds:
            try:
                asset_uuid = UUID(asset_id_str)
            except (ValueError, TypeError) as exc:
                raise ApplicationError(
                    code="INVALID_ARGUMENTS",
                    message=f"assetId 格式无效：{asset_id_str}",
                    status_code=400,
                ) from exc
            asset = await asset_repo.get_by_id(asset_uuid, user_id)
            if asset is None:
                raise ApplicationError(
                    code="ASSET_NOT_FOUND",
                    message="未找到该 Asset 或无权访问。",
                    status_code=404,
                )
            if asset.state != AssetState.READY:
                raise ApplicationError(
                    code="MEDIA_NOT_READY",
                    message=f"Asset {asset_id_str} 尚未就绪，不能关联到 Moment。",
                    status_code=409,
                )
            resolved_assets.append(asset)

    moment = await repo.update(UUID(moment_id), user_id, **fields)
    if moment is None:
        raise ApplicationError(
            code="MOMENT_NOT_FOUND",
            message="未找到该 Moment。",
            status_code=404,
        )
    if resolved_assets is not None:
        link_repo = MomentAssetRepository(session)
        await link_repo.detach_all(moment.id, user_id)
        for position, asset in enumerate(resolved_assets):
            await link_repo.attach(
                user_id=user_id,
                moment_id=moment.id,
                asset_id=asset.id,
                position=position,
                role=AssetRole.ORIGINAL,
            )

    media = await _build_media(
        moment.id, user_id, session, storage, settings, include_download_url=True
    )
    response = _to_dict(moment, media=media)

    # 记录版本快照（只保存稳定 Asset 引用，不保存短期签名 URL）
    revision_repo = SqlMomentRevisionRepository(session)
    await revision_repo.append(
        user_id=user_id,
        moment_id=moment.id,
        revision=moment.revision,
        operation="updated",
        snapshot=_to_dict(moment, media=_revision_media_snapshot(media)),
        actor_user_id=ctx.user_id,
    )

    # 审计
    audit_repo = SqlAuditEventRepository(session)
    await audit_repo.append(
        user_id=user_id,
        actor_type=_actor_type(ctx),
        actor_id=str(ctx.device_id) if ctx.method == "glasses" else ctx.client_id,
        event_type="moment.updated",
        resource_type="moment",
        resource_id=moment.id,
        allowed=True,
    )

    if idem_repo is not None and idem_record is not None:
        await idem_repo.complete(
            record_id=idem_record.id,
            response_status=status.HTTP_200_OK,
            response_body=response,
            resource_id=moment.id,
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
