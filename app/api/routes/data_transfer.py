from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth_context, get_authenticated_user_id
from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.audit_event_repository import SqlAuditEventRepository
from app.infrastructure.database.repositories.confirmation_repository import (
    SqlConfirmationRepository,
)
from app.infrastructure.database.repositories.moment_repository import PostgresMomentRepository
from app.infrastructure.database.repositories.moment_revision_repository import (
    SqlMomentRevisionRepository,
)
from app.infrastructure.database.session import get_db_session
from app.modules.data_transfer.bookkeeping_xlsx import export_workbook, file_digest, parse_workbook
from app.modules.moment_types.registry import validate as validate_moment_type
from app.modules.moments.domain import (
    LocationSource,
    Moment,
    MomentCategory,
    MomentLocation,
    MomentProvenance,
    ProvenanceSource,
)

router = APIRouter(prefix="/v1/data/bookkeeping", tags=["data-transfer"])
MAX_FILE_BYTES = 10 * 1024 * 1024


class ConfirmRequest(BaseModel):
    confirmationId: str


async def _content(file: UploadFile) -> bytes:
    content = await file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ApplicationError(
            code="FILE_TOO_LARGE", message="Excel 文件不能超过 10 MB。", status_code=413
        )
    return content


def _confirmation_error(message: str = "请重新执行预览。") -> ApplicationError:
    return ApplicationError(code="CONFIRMATION_REQUIRED", message=message, status_code=400)


@router.post("/import-preview")
async def import_preview(
    file: UploadFile = File(...),
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    content = await _content(file)
    try:
        parsed = parse_workbook(content)
    except ValueError as exc:
        raise ApplicationError(
            code="UNSUPPORTED_IMPORT_FORMAT", message=str(exc), status_code=400
        ) from exc
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=10)
    confirmation = await SqlConfirmationRepository(session).create(
        user_id=user_id,
        target_type="bookkeeping_import",
        target_id=uuid4(),
        action="import",
        expected_revision=0,
        preview={
            "digest": file_digest(content),
            "format": parsed.format_key,
            "count": len(parsed.rows),
        },
        expires_at=expires_at,
    )
    return {
        "confirmationId": str(confirmation.id),
        "expiresAt": expires_at.isoformat(),
        "count": len(parsed.rows),
        "skippedRows": parsed.skipped_rows,
        "errors": list(parsed.errors),
    }


@router.post("/import-confirm")
async def import_confirm(
    confirmation_id: str = Form(alias="confirmationId"),
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    content = await _content(file)
    confirmations = SqlConfirmationRepository(session)
    confirmation = await confirmations.get(UUID(confirmation_id))
    if (
        confirmation is None
        or confirmation.user_id != ctx.user_id
        or confirmation.target_type != "bookkeeping_import"
        or confirmation.action != "import"
    ):
        raise _confirmation_error()
    if confirmation.status != "pending" or datetime.now(UTC) > confirmation.expires_at:
        raise _confirmation_error("预览已失效，请重新选择文件。")
    if confirmation.preview.get("digest") != file_digest(content):
        raise _confirmation_error("文件与预览时不一致，请重新选择文件。")
    parsed = parse_workbook(content)
    if len(parsed.rows) != confirmation.preview.get("count"):
        raise _confirmation_error("文件内容已经变化，请重新选择文件。")
    repo = PostgresMomentRepository(session)
    revisions = SqlMomentRevisionRepository(session)
    now = datetime.now(UTC)
    for row in parsed.rows:
        validate_moment_type("bookkeeping", row.payload)
        moment = Moment(
            id=uuid4(),
            user_id=ctx.user_id,
            title=row.title,
            description=row.description,
            voice_input=None,
            ai_summary=None,
            category=MomentCategory.EXPERIENCE,
            tags=row.tags,
            persons=(),
            event=None,
            occurred_at=row.occurred_at,
            timezone="Asia/Shanghai",
            revision=1,
            created_at=now,
            updated_at=now,
            location=MomentLocation(name=row.location_name, source=LocationSource.USER)
            if row.location_name
            else None,
            provenance=MomentProvenance(source=ProvenanceSource.WEB),
            moment_type="bookkeeping",
            payload=row.payload,
        )
        created = await repo.create(moment)
        await revisions.append(
            user_id=ctx.user_id,
            moment_id=created.id,
            revision=1,
            operation="created",
            snapshot={
                "id": str(created.id),
                "title": created.title,
                "type": "bookkeeping",
                "payload": created.payload,
            },
            actor_user_id=ctx.user_id,
        )
    await confirmations.mark_used(confirmation_id=confirmation.id, used_at=now)
    await SqlAuditEventRepository(session).append(
        user_id=ctx.user_id,
        actor_type="web",
        actor_id=str(ctx.user_id),
        event_type="bookkeeping.imported",
        resource_type="moment",
        resource_id=None,
        allowed=True,
        metadata={"count": len(parsed.rows), "format": parsed.format_key},
    )
    return {"importedCount": len(parsed.rows)}


@router.get("/export")
async def export_bookkeeping(
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    moments = await PostgresMomentRepository(session).list_all_by_type(user_id, "bookkeeping")
    rows = [
        {
            "id": str(m.id),
            "title": m.title,
            "description": m.description,
            "occurredAt": m.occurred_at.isoformat(),
            "timezone": m.timezone,
            "payload": m.payload,
            "tags": list(m.tags),
            "location": {"name": m.location.name} if m.location else None,
            "createdAt": m.created_at.isoformat(),
            "updatedAt": m.updated_at.isoformat(),
            "revision": m.revision,
        }
        for m in moments
    ]
    filename = f"Moment-One-bookkeeping-{datetime.now(UTC).date().isoformat()}.xlsx"
    return Response(
        export_workbook(rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/clear-preview")
async def clear_preview(
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    count = await PostgresMomentRepository(session).count_by_type(user_id, "bookkeeping")
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)
    confirmation = await SqlConfirmationRepository(session).create(
        user_id=user_id,
        target_type="bookkeeping",
        target_id=uuid4(),
        action="delete_all",
        expected_revision=count,
        preview={"count": count},
        expires_at=expires_at,
    )
    return {
        "confirmationId": str(confirmation.id),
        "expiresAt": expires_at.isoformat(),
        "count": count,
        "confirmationPhrase": "清除全部记账记录",
    }


@router.post("/clear-confirm")
async def clear_confirm(
    body: ConfirmRequest,
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    confirmations = SqlConfirmationRepository(session)
    confirmation = await confirmations.get(UUID(body.confirmationId))
    if (
        confirmation is None
        or confirmation.user_id != user_id
        or confirmation.target_type != "bookkeeping"
        or confirmation.action != "delete_all"
    ):
        raise _confirmation_error()
    if confirmation.status != "pending" or datetime.now(UTC) > confirmation.expires_at:
        raise _confirmation_error("确认已失效，请重新预览。")
    repo = PostgresMomentRepository(session)
    current_count = await repo.count_by_type(user_id, "bookkeeping")
    if current_count != confirmation.expected_revision:
        raise ApplicationError(
            code="REVISION_CONFLICT", message="记录数量已经变化，请重新确认。", status_code=409
        )
    deleted = await repo.soft_delete_all_by_type(user_id, "bookkeeping")
    await confirmations.mark_used(confirmation_id=confirmation.id, used_at=datetime.now(UTC))
    await SqlAuditEventRepository(session).append(
        user_id=user_id,
        actor_type="web",
        actor_id=str(user_id),
        event_type="bookkeeping.cleared",
        resource_type="moment",
        resource_id=None,
        allowed=True,
        metadata={"count": deleted},
    )
    return {"deletedCount": deleted}
