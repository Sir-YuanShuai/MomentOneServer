from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UploadIntent:
    asset_id: str
    method: str
    url: str
    expires_in_seconds: int
    headers: dict[str, str]


class MinioObjectStorage:
    """S3-compatible MinIO adapter boundary.

    Presigned upload and download operations will be implemented after the
    endpoint, bucket and service account policy are provided.
    """
