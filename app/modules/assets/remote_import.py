"""Import short-lived, host-provided attachment URLs into owned Moment One assets."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.asset_repository import AssetRepository
from app.infrastructure.database.repositories.audit_event_repository import (
    SqlAuditEventRepository,
)
from app.infrastructure.storage.object_storage import ObjectStorage
from app.modules.assets.domain import infer_kind
from app.modules.entitlements.repository import EntitlementRepository


@dataclass(frozen=True, slots=True)
class RemoteAttachmentReference:
    name: str
    external_id: str
    mime_type: str
    download_url: str


def validate_remote_attachment_url(url: str, allowed_hosts: tuple[str, ...] | list[str]) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = {item.lower().rstrip(".") for item in allowed_hosts}
    if parsed.scheme != "https" or not host or host not in allowed:
        raise ApplicationError(
            code="UNTRUSTED_ATTACHMENT_SOURCE",
            message="附件来源不在服务器允许的临时资源域名中。",
            status_code=400,
        )
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ApplicationError(
            code="UNTRUSTED_ATTACHMENT_SOURCE",
            message="附件下载地址格式无效。",
            status_code=400,
        )


async def import_remote_attachments(
    refs: list[RemoteAttachmentReference],
    *,
    user_id: UUID,
    session: AsyncSession,
    storage: ObjectStorage,
    allowed_hosts: tuple[str, ...] | list[str],
    configured_max_bytes: int,
    actor_type: str,
    actor_id: str | None,
    provider: str,
) -> list[dict[str, object]]:
    """Fetch allow-listed URLs without redirects and return ready, owned assets."""
    imported: list[dict[str, object]] = []
    quota = EntitlementRepository(session)
    asset_repo = AssetRepository(session)
    plan_limit = await quota.max_upload_bytes(user_id)
    max_bytes = min(configured_max_bytes, plan_limit or configured_max_bytes)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30, connect=10), follow_redirects=False
    ) as client:
        for ref in refs:
            validate_remote_attachment_url(ref.download_url, allowed_hosts)
            content_type = ref.mime_type.lower()
            kind = infer_kind(content_type)
            if kind is None:
                raise ApplicationError(
                    code="MEDIA_TYPE_NOT_ALLOWED",
                    message=f"不支持的附件类型：{ref.mime_type}",
                    status_code=415,
                )
            try:
                async with client.stream("GET", ref.download_url) as response:
                    response.raise_for_status()
                    response_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if response_type and response_type.lower() != content_type:
                        raise ApplicationError(
                            code="MEDIA_UPLOAD_MISMATCH",
                            message="附件实际类型与声明类型不一致。",
                            status_code=422,
                        )
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        raise ApplicationError(
                            code="MEDIA_TOO_LARGE",
                            message=f"附件大小超过上限 {max_bytes} 字节。",
                            status_code=413,
                        )
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > max_bytes:
                            raise ApplicationError(
                                code="MEDIA_TOO_LARGE",
                                message=f"附件大小超过上限 {max_bytes} 字节。",
                                status_code=413,
                            )
            except ApplicationError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                raise ApplicationError(
                    code="ATTACHMENT_IMPORT_FAILED",
                    message="无法从客户端提供的临时资源地址导入附件。",
                    status_code=422,
                ) from exc

            if not payload:
                raise ApplicationError(
                    code="ATTACHMENT_IMPORT_FAILED",
                    message="附件内容为空。",
                    status_code=422,
                )
            await quota.reserve_upload(user_id, len(payload))
            asset = await asset_repo.create(
                user_id=user_id,
                kind=kind,
                content_type=content_type,
                size_bytes=len(payload),
            )
            await asyncio.to_thread(
                storage.put_object_bytes,
                user_id=str(user_id),
                asset_id=str(asset.id),
                data=bytes(payload),
                content_type=content_type,
            )
            await asset_repo.mark_ready(
                asset.id,
                user_id,
                size_bytes=len(payload),
                checksum_sha256=hashlib.sha256(payload).hexdigest(),
            )
            await quota.complete_upload(
                user_id, reserved_bytes=len(payload), actual_bytes=len(payload)
            )
            await SqlAuditEventRepository(session).append(
                user_id=user_id,
                actor_type=actor_type,
                actor_id=actor_id,
                event_type="asset.imported",
                resource_type="asset",
                resource_id=asset.id,
                allowed=True,
                metadata={"provider": provider, "externalFileId": ref.external_id},
            )
            imported.append(
                {
                    "assetId": str(asset.id),
                    "name": ref.name,
                    "kind": kind.value,
                    "contentType": content_type,
                    "sizeBytes": len(payload),
                    "state": "ready",
                }
            )
    return imported


__all__ = [
    "RemoteAttachmentReference",
    "import_remote_attachments",
    "validate_remote_attachment_url",
]
