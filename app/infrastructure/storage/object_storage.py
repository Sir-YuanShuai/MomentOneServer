"""对象存储适配层。

定义 `ObjectStorage` 协议和 S3 兼容实现 `S3ObjectStorage`。

设计要点：
- 客户端不持有 Access Key / Secret Key，只使用服务端签发的短期 Presigned URL。
- Object Key 只能由服务端生成。
- 上传使用 PUT Presigned URL；下载使用 GET Presigned URL。
- complete 阶段通过 head_object 验证对象存在并读取真实 size/content-type。
- 未配置 S3 时返回 `ObjectStorageNotConfigured`，路由层映射为 503。
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.core.config import Settings
from app.modules.assets.domain import build_storage_key


class ObjectStorageNotConfigured(RuntimeError):
    """S3/MinIO 未配置 endpoint/bucket/credentials。"""


@dataclass(frozen=True, slots=True)
class UploadIntent:
    """upload-intent 响应体。"""

    asset_id: str
    method: str
    url: str
    expires_in_seconds: int
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    """head_object 返回的元数据。"""

    size_bytes: int
    content_type: str
    etag: str


class ObjectStorage(Protocol):
    """对象存储协议，便于测试注入 Fake。"""

    def create_upload_intent(
        self,
        *,
        user_id: str,
        asset_id: str,
        content_type: str,
        size_bytes: int,
        expires_in_seconds: int,
    ) -> UploadIntent: ...

    def head_object(self, *, user_id: str, asset_id: str) -> ObjectMetadata: ...

    def create_download_url(
        self,
        *,
        user_id: str,
        asset_id: str,
        expires_in_seconds: int,
    ) -> str: ...


class S3ObjectStorage:
    """S3 / MinIO 兼容适配器（基于 boto3）。"""

    def __init__(self, settings: Settings) -> None:
        if not settings.s3_bucket or not settings.s3_access_key or not settings.s3_secret_key:
            raise ObjectStorageNotConfigured("S3 bucket/access_key/secret_key 未配置")

        import boto3
        from botocore.client import Config  # type: ignore[import-not-found]

        endpoint = str(settings.s3_endpoint_url) if settings.s3_endpoint_url else None
        self._bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
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
        key = build_storage_key(UUID(user_id), UUID(asset_id))
        url = self._client.generate_presigned_put_object(
            Bucket=self._bucket,
            Key=key,
            ExpiresIn=expires_in_seconds,
            ContentType=content_type,
        )
        # 客户端 PUT 时必须携带的 header（与签发时一致）
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(size_bytes),
        }
        return UploadIntent(
            asset_id=asset_id,
            method="PUT",
            url=url,
            expires_in_seconds=expires_in_seconds,
            headers=headers,
        )

    def head_object(self, *, user_id: str, asset_id: str) -> ObjectMetadata:
        key = build_storage_key(UUID(user_id), UUID(asset_id))
        resp = self._client.head_object(Bucket=self._bucket, Key=key)
        size = int(resp.get("ContentLength", 0))
        content_type = str(resp.get("ContentType", "application/octet-stream"))
        etag = str(resp.get("ETag", "")).strip('"')
        return ObjectMetadata(size_bytes=size, content_type=content_type, etag=etag)

    def create_download_url(
        self,
        *,
        user_id: str,
        asset_id: str,
        expires_in_seconds: int,
    ) -> str:
        key = build_storage_key(UUID(user_id), UUID(asset_id))
        return self._client.generate_presigned_get_object(
            Bucket=self._bucket,
            Key=key,
            ExpiresIn=expires_in_seconds,
        )


def get_object_storage(settings: Settings) -> ObjectStorage:
    """工厂：根据配置返回 S3ObjectStorage 或抛 ObjectStorageNotConfigured。"""
    return S3ObjectStorage(settings)


__all__ = [
    "ObjectMetadata",
    "ObjectStorage",
    "ObjectStorageNotConfigured",
    "S3ObjectStorage",
    "UploadIntent",
    "get_object_storage",
]
