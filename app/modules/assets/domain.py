"""Asset 领域模型：媒体业务元数据。

实际字节存放在 MinIO/S3，本模块只承载业务状态、所有权和引用关系。
状态机：uploading -> ready | failed；ready -> detached；failed/detached -> purged。
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class AssetState(StrEnum):
    UPLOADING = "uploading"
    READY = "ready"
    DETACHED = "detached"
    FAILED = "failed"
    PURGED = "purged"


class AssetKind(StrEnum):
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"


class AssetRole(StrEnum):
    ORIGINAL = "original"
    COVER = "cover"
    VOICE_NOTE = "voice_note"
    ATTACHMENT = "attachment"


# Content-Type -> AssetKind 映射，用于 upload-intent 时校验和推断
_KIND_BY_CONTENT_TYPE: dict[str, AssetKind] = {
    "image/jpeg": AssetKind.IMAGE,
    "image/png": AssetKind.IMAGE,
    "image/webp": AssetKind.IMAGE,
    "image/gif": AssetKind.IMAGE,
    "audio/mpeg": AssetKind.AUDIO,
    "audio/mp3": AssetKind.AUDIO,
    "audio/wav": AssetKind.AUDIO,
    "audio/x-wav": AssetKind.AUDIO,
    "audio/ogg": AssetKind.AUDIO,
    "audio/webm": AssetKind.AUDIO,
    "video/mp4": AssetKind.VIDEO,
    "video/webm": AssetKind.VIDEO,
    "video/quicktime": AssetKind.VIDEO,
    "application/pdf": AssetKind.DOCUMENT,
}


def infer_kind(content_type: str) -> AssetKind | None:
    """根据 Content-Type 推断 AssetKind；不在白名单返回 None。"""
    return _KIND_BY_CONTENT_TYPE.get(content_type.lower())


def allowed_content_types() -> frozenset[str]:
    return frozenset(_KIND_BY_CONTENT_TYPE.keys())


@dataclass(frozen=True, slots=True)
class Asset:
    id: UUID
    user_id: UUID
    state: AssetState
    kind: AssetKind
    storage_key: str
    content_type: str
    size_bytes: int | None
    checksum_sha256: str | None
    created_at: datetime
    ready_at: datetime | None
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class MomentAssetLink:
    """Moment 与 Asset 的关联记录。"""

    user_id: UUID
    moment_id: UUID
    asset_id: UUID
    position: int
    role: AssetRole
    created_at: datetime


def build_storage_key(user_id: UUID, asset_id: UUID, suffix: str = "original") -> str:
    """生成 MinIO/S3 对象 Key。

    格式：users/{user_id}/assets/{asset_id}/{suffix}
    """
    return f"users/{user_id}/assets/{asset_id}/{suffix}"


__all__ = [
    "Asset",
    "AssetKind",
    "AssetRole",
    "AssetState",
    "MomentAssetLink",
    "allowed_content_types",
    "build_storage_key",
    "infer_kind",
]
