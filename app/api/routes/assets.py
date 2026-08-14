"""Asset 路由：媒体上传/确认/查询/下载。

流程：
1. POST /v1/assets/upload-intents  → 创建 Asset(state=uploading) + 返回 Presigned PUT URL
2. Client 直传 S3/MinIO
3. POST /v1/assets/{assetId}/complete → head_object 校验 → state=ready
4. GET  /v1/assets/{assetId}          → 元数据
5. POST /v1/assets/{assetId}/download-url → 短期 GET Presigned URL

未配置 S3 时返回 503 SERVICE_UNAVAILABLE。
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_authenticated_user_id
from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.asset_repository import (
    AssetRepository,
)
from app.infrastructure.database.repositories.audit_event_repository import (
    SqlAuditEventRepository,
)
from app.infrastructure.database.session import get_db_session
from app.infrastructure.storage.object_storage import (
    ObjectMetadata,
    ObjectStorage,
    ObjectStorageNotConfigured,
    UploadIntent,
    get_object_storage,
)
from app.modules.assets.domain import AssetKind, AssetState, infer_kind
from app.modules.assets.thumbnail import (
    THUMBNAIL_CONTENT_TYPE,
    generate_thumbnail,
)
from app.modules.entitlements.repository import EntitlementRepository

router = APIRouter(prefix="/v1/assets", tags=["assets"])

logger = logging.getLogger(__name__)


# ---------- 依赖 ----------


def get_entitlement_repository(
    session: AsyncSession = Depends(get_db_session),
) -> EntitlementRepository:
    return EntitlementRepository(session)


def get_storage(
    settings: Settings = Depends(get_settings),
) -> ObjectStorage:
    """对象存储依赖。未配置时返回一个标记实例，路由层抛 503。

    测试时通过 app.dependency_overrides[get_storage] 注入 Fake。
    """
    try:
        return get_object_storage(settings)
    except ObjectStorageNotConfigured:
        # 返回一个 sentinel，路由检测后抛 503
        return _UnconfiguredStorage()


class _UnconfiguredStorage:
    """S3 未配置时的占位实现，路由层检测后抛 503。"""

    def _raise(self) -> None:
        raise ApplicationError(
            code="SERVICE_UNAVAILABLE",
            message="对象存储未配置，媒体功能不可用。",
            status_code=503,
        )

    def create_upload_intent(
        self,
        *,
        user_id: str,
        asset_id: str,
        content_type: str,
        size_bytes: int,
        expires_in_seconds: int,
    ) -> UploadIntent:
        self._raise()
        raise AssertionError  # pragma: no cover

    def head_object(self, *, user_id: str, asset_id: str) -> ObjectMetadata:
        self._raise()
        raise AssertionError  # pragma: no cover

    def get_object_bytes(self, *, user_id: str, asset_id: str) -> bytes:
        self._raise()
        raise AssertionError  # pragma: no cover

    def put_object_bytes(
        self, *, user_id: str, asset_id: str, data: bytes, content_type: str
    ) -> None:
        self._raise()

    def put_thumbnail(
        self,
        *,
        user_id: str,
        asset_id: str,
        data: bytes,
        content_type: str,
    ) -> None:
        self._raise()
        raise AssertionError  # pragma: no cover

    def create_thumbnail_url(
        self,
        *,
        user_id: str,
        asset_id: str,
        expires_in_seconds: int,
    ) -> str:
        self._raise()
        raise AssertionError  # pragma: no cover

    def create_download_url(
        self,
        *,
        user_id: str,
        asset_id: str,
        expires_in_seconds: int,
    ) -> str:
        self._raise()
        raise AssertionError  # pragma: no cover

    def delete_asset_objects(self, *, user_id: str, asset_id: str) -> None:
        self._raise()


# ---------- 请求/响应模型 ----------


class UploadIntentRequest(BaseModel):
    contentType: str = Field(description="MIME 类型，必须在白名单内")
    sizeBytes: int = Field(gt=0, description="文件大小（字节）")


class UploadIntentResponse(BaseModel):
    assetId: str
    method: str
    url: str
    expiresIn: int
    headers: dict[str, str]
    storageKey: str


class CompleteUploadRequest(BaseModel):
    checksumSha256: str | None = Field(default=None, description="可选 SHA256 校验值")


class CompleteUploadResponse(BaseModel):
    assetId: str
    state: str
    sizeBytes: int
    contentType: str


class AssetMetadataResponse(BaseModel):
    assetId: str
    state: str
    kind: str
    contentType: str
    sizeBytes: int | None
    createdAt: str
    readyAt: str | None


class DownloadUrlResponse(BaseModel):
    assetId: str
    url: str
    expiresIn: int
    expiresAt: str


# ---------- 路由 ----------


@router.post(
    "/upload-intents", response_model=UploadIntentResponse, status_code=status.HTTP_201_CREATED
)
async def create_upload_intent(
    body: UploadIntentRequest,
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    quota_repo: EntitlementRepository = Depends(get_entitlement_repository),
) -> UploadIntentResponse:
    kind = infer_kind(body.contentType)
    if kind is None:
        raise ApplicationError(
            code="MEDIA_TYPE_NOT_ALLOWED",
            message=f"不支持的媒体类型：{body.contentType}",
            status_code=415,
        )
    if body.sizeBytes > settings.max_upload_bytes:
        raise ApplicationError(
            code="MEDIA_TOO_LARGE",
            message=f"文件大小超过上限 {settings.max_upload_bytes} 字节。",
            status_code=413,
        )

    plan_upload_limit = await quota_repo.max_upload_bytes(user_id)
    effective_upload_limit = min(
        settings.max_upload_bytes,
        plan_upload_limit if plan_upload_limit is not None else settings.max_upload_bytes,
    )
    if body.sizeBytes > effective_upload_limit:
        raise ApplicationError(
            code="MEDIA_TOO_LARGE",
            message=f"当前套餐单文件上限为 {effective_upload_limit} 字节。",
            status_code=413,
            details={"maxUploadBytes": effective_upload_limit},
        )
    await quota_repo.reserve_upload(user_id, body.sizeBytes)

    repo = AssetRepository(session)
    asset = await repo.create(
        user_id=user_id,
        kind=kind,
        content_type=body.contentType,
        size_bytes=body.sizeBytes,
    )

    intent = storage.create_upload_intent(
        user_id=str(user_id),
        asset_id=str(asset.id),
        content_type=body.contentType,
        size_bytes=body.sizeBytes,
        expires_in_seconds=settings.s3_upload_url_ttl_seconds,
    )

    # 审计
    audit_repo = SqlAuditEventRepository(session)
    await audit_repo.append(
        user_id=user_id,
        actor_type="web",
        actor_id=str(user_id),
        event_type="asset.upload_intent_created",
        resource_type="asset",
        resource_id=asset.id,
        allowed=True,
    )

    return UploadIntentResponse(
        assetId=str(asset.id),
        method=intent.method,
        url=intent.url,
        expiresIn=intent.expires_in_seconds,
        headers=intent.headers,
        storageKey=asset.storage_key,
    )


@router.post("/{asset_id}/complete", response_model=CompleteUploadResponse)
async def complete_upload(
    asset_id: str,
    body: CompleteUploadRequest,
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
    storage: ObjectStorage = Depends(get_storage),
    quota_repo: EntitlementRepository = Depends(get_entitlement_repository),
) -> CompleteUploadResponse:
    asset_uuid = _parse_uuid(asset_id)
    repo = AssetRepository(session)
    asset = await repo.get_by_id(asset_uuid, user_id)
    if asset is None:
        raise ApplicationError(
            code="ASSET_NOT_FOUND",
            message="未找到该 Asset。",
            status_code=404,
        )
    if asset.state == AssetState.READY:
        # 幂等：已 ready 直接返回
        return CompleteUploadResponse(
            assetId=str(asset.id),
            state=asset.state.value,
            sizeBytes=asset.size_bytes or 0,
            contentType=asset.content_type,
        )
    if asset.state != AssetState.UPLOADING:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message=f"Asset 状态 {asset.state.value} 不允许 complete。",
            status_code=400,
        )

    reserved_bytes = asset.size_bytes or 0

    # head_object 验证对象存在。失败状态和额度释放必须先提交，否则异常响应会回滚事务。
    try:
        meta = storage.head_object(user_id=str(user_id), asset_id=str(asset.id))
    except Exception as exc:
        await repo.mark_failed(asset_uuid, user_id)
        await quota_repo.release_upload(user_id, reserved_bytes=reserved_bytes)
        await session.commit()
        raise ApplicationError(
            code="MEDIA_NOT_READY",
            message="对象存储中未找到上传对象。",
            status_code=422,
        ) from exc

    if (
        meta.size_bytes <= 0
        or meta.size_bytes > reserved_bytes
        or meta.content_type != asset.content_type
    ):
        await repo.mark_failed(asset_uuid, user_id)
        await quota_repo.release_upload(user_id, reserved_bytes=reserved_bytes)
        await session.commit()
        raise ApplicationError(
            code="MEDIA_UPLOAD_MISMATCH",
            message="上传对象与 Upload Intent 声明不一致。",
            status_code=422,
            details={
                "expectedMaxBytes": reserved_bytes,
                "actualBytes": meta.size_bytes,
                "expectedContentType": asset.content_type,
                "actualContentType": meta.content_type,
            },
        )

    asset = await repo.mark_ready(
        asset_uuid,
        user_id,
        size_bytes=meta.size_bytes,
        checksum_sha256=body.checksumSha256,
    )
    if asset is None:
        raise ApplicationError(
            code="ASSET_NOT_FOUND",
            message="未找到该 Asset。",
            status_code=404,
        )

    await quota_repo.complete_upload(
        user_id, reserved_bytes=reserved_bytes, actual_bytes=meta.size_bytes
    )

    # 生成缩略图（仅 image 类）。失败只降级不影响上传成功——原图已 ready。
    if asset.kind == AssetKind.IMAGE:
        try:
            generated = await asyncio.to_thread(
                _generate_thumbnail, storage, str(user_id), str(asset.id)
            )
            if generated:
                await repo.mark_thumbnail_ready(asset_uuid, user_id)
                audit_repo = SqlAuditEventRepository(session)
                await audit_repo.append(
                    user_id=user_id,
                    actor_type="web",
                    actor_id=str(user_id),
                    event_type="asset.thumbnail_generated",
                    resource_type="asset",
                    resource_id=asset.id,
                    allowed=True,
                )
        except Exception as exc:
            logger.warning("缩略图生成失败，已降级：asset_id=%s err=%s", asset.id, exc)

    audit_repo = SqlAuditEventRepository(session)
    await audit_repo.append(
        user_id=user_id,
        actor_type="web",
        actor_id=str(user_id),
        event_type="asset.upload_completed",
        resource_type="asset",
        resource_id=asset.id,
        allowed=True,
    )

    return CompleteUploadResponse(
        assetId=str(asset.id),
        state=asset.state.value,
        sizeBytes=asset.size_bytes or 0,
        contentType=asset.content_type,
    )


@router.get("/{asset_id}", response_model=AssetMetadataResponse)
async def get_asset(
    asset_id: str,
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> AssetMetadataResponse:
    asset_uuid = _parse_uuid(asset_id)
    repo = AssetRepository(session)
    asset = await repo.get_by_id(asset_uuid, user_id)
    if asset is None:
        raise ApplicationError(
            code="ASSET_NOT_FOUND",
            message="未找到该 Asset。",
            status_code=404,
        )
    return AssetMetadataResponse(
        assetId=str(asset.id),
        state=asset.state.value,
        kind=asset.kind.value,
        contentType=asset.content_type,
        sizeBytes=asset.size_bytes,
        createdAt=asset.created_at.isoformat(),
        readyAt=asset.ready_at.isoformat() if asset.ready_at else None,
    )


@router.post("/{asset_id}/download-url", response_model=DownloadUrlResponse)
async def create_download_url(
    asset_id: str,
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> DownloadUrlResponse:
    asset_uuid = _parse_uuid(asset_id)
    repo = AssetRepository(session)
    asset = await repo.get_by_id(asset_uuid, user_id)
    if asset is None:
        raise ApplicationError(
            code="ASSET_NOT_FOUND",
            message="未找到该 Asset。",
            status_code=404,
        )
    if asset.state != AssetState.READY:
        raise ApplicationError(
            code="MEDIA_NOT_READY",
            message="Asset 尚未就绪，无法下载。",
            status_code=409,
        )

    url = storage.create_download_url(
        user_id=str(user_id),
        asset_id=str(asset.id),
        expires_in_seconds=settings.s3_download_url_ttl_seconds,
    )
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.s3_download_url_ttl_seconds)

    return DownloadUrlResponse(
        assetId=str(asset.id),
        url=url,
        expiresIn=settings.s3_download_url_ttl_seconds,
        expiresAt=expires_at.isoformat(),
    )


# ---------- 工具 ----------


def _generate_thumbnail(storage: ObjectStorage, user_id: str, asset_id: str) -> bool:
    """从原图生成缩略图并写回对象存储（同步，供 asyncio.to_thread 调用）。

    返回是否成功生成（图片损坏/像素超限返回 False，静默跳过）；
    存储异常向上抛，由路由层捕获降级。
    """
    data = storage.get_object_bytes(user_id=user_id, asset_id=asset_id)
    thumb = generate_thumbnail(data)
    if thumb is None:
        return False
    storage.put_thumbnail(
        user_id=user_id,
        asset_id=asset_id,
        data=thumb,
        content_type=THUMBNAIL_CONTENT_TYPE,
    )
    return True


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, TypeError) as exc:
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="assetId 格式无效。",
            status_code=400,
        ) from exc


__all__ = ["router"]
