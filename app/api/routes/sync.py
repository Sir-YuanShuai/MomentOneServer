"""离线优先客户端的业务操作批量同步入口。

同步入口只负责协议编排与逐条结果，不复制 Moment 的领域规则；具体创建/更新仍复用
既有用例。第一期仅接受无媒体的 Moment 创建与更新，删除继续走 Preview + Confirm。
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth_context
from app.api.routes.moments import (
    CreateMomentRequest,
    UpdateMomentRequest,
    create_moment,
    get_storage,
    update_moment,
)
from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.idempotency_repository import (
    SqlIdempotencyRepository,
    fingerprint_payload,
)
from app.infrastructure.database.repositories.sync_change_repository import (
    SqlSyncChangeRepository,
)
from app.infrastructure.database.session import get_db_session
from app.infrastructure.storage.object_storage import ObjectStorage

router = APIRouter(prefix="/v1/sync", tags=["sync"])


class SyncOperation(BaseModel):
    operationId: UUID
    kind: Literal["moment.create", "moment.update"]
    idempotencyKey: str = Field(min_length=1, max_length=255)
    entityId: UUID
    expectedRevision: int | None = Field(default=None, ge=1)
    payload: dict = Field(default_factory=dict)


class SyncOperationsRequest(BaseModel):
    operations: list[SyncOperation] = Field(min_length=1, max_length=100)


class SyncChangesResponse(BaseModel):
    changes: list[dict]
    nextCursor: str | None
    hasMore: bool


def _error_status(error: ApplicationError) -> Literal["conflict", "rejected", "retryable"]:
    if error.status_code == 409:
        return "conflict"
    if error.status_code >= 500:
        return "retryable"
    return "rejected"


@router.get("/changes", response_model=SyncChangesResponse)
async def sync_changes(
    cursor: int | None = None,
    limit: int = 100,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> SyncChangesResponse:
    """读取当前用户的 Moment 增量变更和 Tombstone。

    游标是服务端日志序号；客户端只将它当作不透明字符串保存，不以设备时钟或
    `updatedAt` 判断先后。多余取一条用于判断是否还有下一页。
    """

    if cursor is not None and cursor < 0:
        raise ApplicationError(code="INVALID_ARGUMENTS", message="同步游标无效。", status_code=400)
    repository = SqlSyncChangeRepository(session)
    resolved_limit = min(max(limit, 1), 100)
    changes = await repository.list_after(
        user_id=ctx.user_id,
        sequence=cursor or 0,
        limit=resolved_limit + 1,
    )
    has_more = len(changes) > resolved_limit
    page = changes[:resolved_limit]
    return SyncChangesResponse(
        changes=[
            {
                "cursor": str(change.sequence),
                "entityType": change.entity_type,
                "entityId": str(change.entity_id),
                "operation": change.operation,
                "revision": change.revision,
                "entity": change.snapshot,
            }
            for change in page
        ],
        nextCursor=str(page[-1].sequence) if page else (str(cursor) if cursor else None),
        hasMore=has_more,
    )


@router.post("/operations")
async def sync_operations(
    body: SyncOperationsRequest,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
    storage: ObjectStorage | None = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict:
    """提交一批本地操作；每条独立返回，避免单个冲突阻断其他记录。"""

    operation_ids = [operation.operationId for operation in body.operations]
    if len(operation_ids) != len(set(operation_ids)):
        raise ApplicationError(
            code="INVALID_ARGUMENTS",
            message="同步批次不能包含重复 operationId。",
            status_code=400,
        )

    results: list[dict] = []
    for operation in body.operations:
        try:
            # 业务校验失败、完整性异常都只回滚当前操作，不能污染同批后续操作。
            async with session.begin_nested():
                if operation.kind == "moment.create":
                    payload = dict(operation.payload)
                    payload.pop("id", None)
                    if payload.get("assetIds"):
                        raise ApplicationError(
                            code="INVALID_ARGUMENTS",
                            message="离线同步暂不支持包含媒体附件的记录。",
                            status_code=400,
                        )
                    payload.pop("assetIds", None)
                    request = CreateMomentRequest(
                        **payload,
                        id=operation.entityId,
                        assetIds=[],
                    )
                    entity = await create_moment(
                        request,
                        ctx,
                        session,
                        storage,
                        settings,
                        operation.idempotencyKey,
                    )
                else:
                    if operation.expectedRevision is None:
                        raise ApplicationError(
                            code="EXPECTED_REVISION_REQUIRED",
                            message="更新同步操作必须提供 expectedRevision。",
                            status_code=400,
                        )
                    payload = dict(operation.payload)
                    if "assetIds" in payload:
                        raise ApplicationError(
                            code="INVALID_ARGUMENTS",
                            message="离线同步暂不支持修改媒体附件。",
                            status_code=400,
                        )
                    payload.pop("expectedRevision", None)
                    sync_payload = {
                        "entityId": str(operation.entityId),
                        "expectedRevision": operation.expectedRevision,
                        "payload": payload,
                    }
                    idem = SqlIdempotencyRepository(session)
                    record = await idem.acquire(
                        user_id=ctx.user_id,
                        operation="sync_moment_update",
                        idempotency_key=operation.idempotencyKey,
                        request_payload=sync_payload,
                    )
                    if record.request_fingerprint != fingerprint_payload(sync_payload):
                        raise ApplicationError(
                            code="IDEMPOTENCY_CONFLICT",
                            message="Idempotency-Key 已用于不同的同步操作。",
                            status_code=409,
                        )
                    if record.state == "completed" and record.response_body is not None:
                        entity = record.response_body
                    else:
                        request = UpdateMomentRequest(
                            **payload,
                            expectedRevision=operation.expectedRevision,
                            assetIds=None,
                        )
                        entity = await update_moment(
                            str(operation.entityId), request, ctx, session, storage, settings
                        )
                        await idem.complete(
                            record_id=record.id,
                            response_status=200,
                            response_body=entity,
                            resource_id=operation.entityId,
                        )
            results.append(
                {
                    "operationId": str(operation.operationId),
                    "status": "applied",
                    "entity": entity,
                }
            )
        except ApplicationError as error:
            result: dict = {
                "operationId": str(operation.operationId),
                "status": _error_status(error),
                "code": error.code,
                "message": error.message,
            }
            if error.details:
                result["details"] = error.details
            results.append(result)
        except SQLAlchemyError:
            # 单条数据库异常已由 savepoint 隔离；本地操作保留到下次再试。
            results.append(
                {
                    "operationId": str(operation.operationId),
                    "status": "retryable",
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "同步服务暂时不可用，请稍后重试。",
                }
            )

    return {"results": results}
