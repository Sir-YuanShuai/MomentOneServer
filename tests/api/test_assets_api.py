"""Asset 路由 API 测试。

通过 dependency_overrides 注入 Fake AuthContext、内存态 Repository 和 FakeStorage，
不依赖数据库、MinIO/S3 和 Casdoor。覆盖：
- POST /v1/assets/upload-intents （创建 + presigned URL + 类型/大小校验）
- POST /v1/assets/{assetId}/complete （head_object 校验 + 状态机 + 幂等）
- GET  /v1/assets/{assetId} （元数据查询 + 404）
- POST /v1/assets/{assetId}/download-url （仅 READY 可下载 + 404 + 409）
- POST /v1/moments 含 assetIds 时建立关联 + 响应 media 数组
"""

import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.api import deps as deps_module
from app.api.routes import assets as assets_routes
from app.api.routes import moments as moments_routes
from app.application import create_application
from app.core.config import Settings
from app.infrastructure.database.repositories.confirmation_repository import (
    PendingConfirmation,
)
from app.infrastructure.database.repositories.idempotency_repository import (
    IdempotencyRecord,
)
from app.infrastructure.storage.object_storage import (
    ObjectMetadata,
    ObjectStorage,
    UploadIntent,
)
from app.modules.assets.domain import (
    Asset,
    AssetKind,
    AssetRole,
    AssetState,
    MomentAssetLink,
    build_storage_key,
)
from app.modules.moments.domain import Moment
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

USER_ID = UUID("22222222-2222-4222-8222-222222222222")

# 1x1 红色 PNG，用于缩略图生成测试（真实可解码图片字节）
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _generate_rsa_keypair(tmp_path: Path) -> tuple[Path, Path]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path = tmp_path / "jwt_private.pem"
    pub_path = tmp_path / "jwt_public.pem"
    priv_path.write_bytes(priv_pem)
    pub_path.write_bytes(pub_pem)
    return priv_path, pub_path


def _make_settings(tmp_path: Path) -> Settings:
    priv_path, pub_path = _generate_rsa_keypair(tmp_path)
    return Settings(
        env="test",
        jwt_private_key_path=str(priv_path),
        jwt_public_key_path=str(pub_path),
        jwt_issuer="https://momentone.test",
        jwt_audience="momentone-glasses",
        access_token_ttl_seconds=3600,
        refresh_token_ttl_seconds=90 * 24 * 3600,
        binding_code_ttl_seconds=300,
        binding_code_length=24,
    )


# ---------- Fake repositories ----------


class FakeAssetRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, Asset] = {}

    async def create(
        self, *, user_id: UUID, kind: AssetKind, content_type: str, size_bytes: int | None = None
    ) -> Asset:
        asset_id = uuid4()
        asset = Asset(
            id=asset_id,
            user_id=user_id,
            state=AssetState.UPLOADING,
            kind=kind,
            storage_key=build_storage_key(user_id, asset_id),
            content_type=content_type,
            size_bytes=size_bytes,
            checksum_sha256=None,
            created_at=datetime.now(UTC),
            ready_at=None,
            thumbnail_generated_at=None,
            deleted_at=None,
        )
        self._store[asset_id] = asset
        return asset

    async def get_by_id(self, asset_id: UUID, user_id: UUID) -> Asset | None:
        a = self._store.get(asset_id)
        if a is None or a.user_id != user_id or a.deleted_at is not None:
            return None
        return a

    async def mark_ready(
        self,
        asset_id: UUID,
        user_id: UUID,
        *,
        size_bytes: int,
        checksum_sha256: str | None = None,
    ) -> Asset | None:
        a = self._store.get(asset_id)
        if a is None or a.user_id != user_id:
            return None
        self._store[asset_id] = Asset(
            id=a.id,
            user_id=a.user_id,
            state=AssetState.READY,
            kind=a.kind,
            storage_key=a.storage_key,
            content_type=a.content_type,
            size_bytes=size_bytes,
            checksum_sha256=checksum_sha256,
            created_at=a.created_at,
            ready_at=datetime.now(UTC),
            thumbnail_generated_at=a.thumbnail_generated_at,
            deleted_at=None,
        )
        return self._store[asset_id]

    async def mark_thumbnail_ready(self, asset_id: UUID, user_id: UUID) -> Asset | None:
        a = self._store.get(asset_id)
        if a is None or a.user_id != user_id:
            return None
        self._store[asset_id] = Asset(
            id=a.id,
            user_id=a.user_id,
            state=a.state,
            kind=a.kind,
            storage_key=a.storage_key,
            content_type=a.content_type,
            size_bytes=a.size_bytes,
            checksum_sha256=a.checksum_sha256,
            created_at=a.created_at,
            ready_at=a.ready_at,
            thumbnail_generated_at=datetime.now(UTC),
            deleted_at=None,
        )
        return self._store[asset_id]

    async def mark_failed(self, asset_id: UUID, user_id: UUID) -> Asset | None:
        a = self._store.get(asset_id)
        if a is None or a.user_id != user_id:
            return None
        self._store[asset_id] = Asset(
            id=a.id,
            user_id=a.user_id,
            state=AssetState.FAILED,
            kind=a.kind,
            storage_key=a.storage_key,
            content_type=a.content_type,
            size_bytes=a.size_bytes,
            checksum_sha256=a.checksum_sha256,
            created_at=a.created_at,
            ready_at=None,
            thumbnail_generated_at=a.thumbnail_generated_at,
            deleted_at=None,
        )
        return self._store[asset_id]


class FakeMomentAssetRepository:
    def __init__(self) -> None:
        self._links: list[MomentAssetLink] = []

    async def attach(
        self,
        *,
        user_id: UUID,
        moment_id: UUID,
        asset_id: UUID,
        position: int,
        role: AssetRole = AssetRole.ORIGINAL,
    ) -> MomentAssetLink:
        link = MomentAssetLink(
            user_id=user_id,
            moment_id=moment_id,
            asset_id=asset_id,
            position=position,
            role=role,
            created_at=datetime.now(UTC),
        )
        self._links.append(link)
        return link

    async def list_by_moment(self, moment_id: UUID, user_id: UUID) -> list[MomentAssetLink]:
        return [
            link for link in self._links if link.moment_id == moment_id and link.user_id == user_id
        ]

    async def detach_all(self, moment_id: UUID, user_id: UUID) -> int:
        before = len(self._links)
        self._links = [
            link
            for link in self._links
            if not (link.moment_id == moment_id and link.user_id == user_id)
        ]
        return before - len(self._links)


class FakeMomentRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, Moment] = {}

    async def create(self, moment: Moment) -> Moment:
        self._store[moment.id] = moment
        return moment

    async def get_by_id(self, moment_id: UUID, user_id: UUID) -> Moment | None:
        m = self._store.get(moment_id)
        if m is None or m.user_id != user_id or m.deleted_at is not None:
            return None
        return m

    async def list_by_user(
        self,
        *,
        user_id: UUID,
        limit: int,
        cursor: str | None,
        moment_type: str | None = None,
        category: str | None = None,
        tag: str | None = None,
        goal_id: UUID | None = None,
    ) -> tuple[list[Moment], bool, str | None]:
        items = [m for m in self._store.values() if m.user_id == user_id]
        if moment_type:
            items = [m for m in items if m.moment_type == moment_type]
        if category:
            items = [m for m in items if m.category.value == category]
        if tag:
            items = [m for m in items if tag in m.tags]
        if goal_id:
            items = [m for m in items if m.payload.get("goalId") == str(goal_id)]
        return items[:limit], len(items) > limit, None

    async def update(self, moment_id: UUID, user_id: UUID, **fields: Any) -> Moment | None:
        m = self._store.get(moment_id)
        if m is None or m.user_id != user_id:
            return None
        updated = Moment(
            id=m.id,
            user_id=m.user_id,
            title=fields.get("title", m.title),
            description=fields.get("description", m.description),
            voice_input=fields.get("voice_input", m.voice_input),
            ai_summary=fields.get("ai_summary", m.ai_summary),
            category=fields.get("category", m.category),
            tags=fields.get("tags", m.tags),
            occurred_at=fields.get("occurred_at", m.occurred_at),
            timezone=fields.get("timezone", m.timezone),
            revision=m.revision + 1,
            created_at=m.created_at,
            updated_at=datetime.now(UTC),
            location=fields.get("location", m.location),
            emotion=fields.get("emotion", m.emotion),
            provenance=m.provenance,
            deleted_at=m.deleted_at,
        )
        self._store[moment_id] = updated
        return updated

    async def soft_delete(self, moment_id: UUID, user_id: UUID) -> Moment | None:
        m = self._store.get(moment_id)
        if m is None or m.user_id != user_id:
            return None
        deleted = Moment(
            id=m.id,
            user_id=m.user_id,
            title=m.title,
            description=m.description,
            voice_input=m.voice_input,
            ai_summary=m.ai_summary,
            category=m.category,
            tags=m.tags,
            occurred_at=m.occurred_at,
            timezone=m.timezone,
            revision=m.revision + 1,
            created_at=m.created_at,
            updated_at=datetime.now(UTC),
            location=m.location,
            emotion=m.emotion,
            provenance=m.provenance,
            deleted_at=datetime.now(UTC),
        )
        self._store[moment_id] = deleted
        return deleted


class FakeConfirmationRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, PendingConfirmation] = {}

    async def create(
        self,
        *,
        user_id: UUID,
        target_type: str,
        target_id: UUID,
        action: str,
        expected_revision: int,
        preview: dict,
        expires_at: datetime,
    ) -> PendingConfirmation:
        cid = uuid4()
        c = PendingConfirmation(
            id=cid,
            user_id=user_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            expected_revision=expected_revision,
            status="pending",
            preview=preview,
            created_at=datetime.now(UTC),
            expires_at=expires_at,
            used_at=None,
        )
        self._store[cid] = c
        return c

    async def get(self, confirmation_id: UUID) -> PendingConfirmation | None:
        return self._store.get(confirmation_id)

    async def mark_used(self, *, confirmation_id: UUID, used_at: datetime) -> None:
        c = self._store.get(confirmation_id)
        if c is None:
            return
        self._store[confirmation_id] = PendingConfirmation(
            id=c.id,
            user_id=c.user_id,
            target_type=c.target_type,
            target_id=c.target_id,
            action=c.action,
            expected_revision=c.expected_revision,
            status="used",
            preview=c.preview,
            created_at=c.created_at,
            expires_at=c.expires_at,
            used_at=used_at,
        )


class FakeIdempotencyRepository:
    def __init__(self) -> None:
        self._store: dict[tuple[UUID, str, str], IdempotencyRecord] = {}

    async def acquire(
        self,
        *,
        user_id: UUID,
        operation: str,
        idempotency_key: str,
        request_payload: dict,
        ttl: timedelta = timedelta(hours=24),
    ) -> IdempotencyRecord:
        import hashlib
        import json

        canonical = json.dumps(
            request_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        fp = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        key = (user_id, operation, idempotency_key)
        existing = self._store.get(key)
        now = datetime.now(UTC)
        if existing is not None:
            if now > existing.expires_at:
                rec = IdempotencyRecord(
                    id=existing.id,
                    user_id=user_id,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fp,
                    state="processing",
                    response_status=None,
                    response_body=None,
                    resource_id=None,
                    created_at=existing.created_at,
                    expires_at=now + ttl,
                )
                self._store[key] = rec
                return rec
            return existing
        rec = IdempotencyRecord(
            id=uuid4(),
            user_id=user_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=fp,
            state="processing",
            response_status=None,
            response_body=None,
            resource_id=None,
            created_at=now,
            expires_at=now + ttl,
        )
        self._store[key] = rec
        return rec

    async def complete(
        self,
        *,
        record_id: UUID,
        response_status: int,
        response_body: dict,
        resource_id: UUID | None = None,
    ) -> None:
        for key, rec in self._store.items():
            if rec.id == record_id:
                self._store[key] = IdempotencyRecord(
                    id=rec.id,
                    user_id=rec.user_id,
                    operation=rec.operation,
                    idempotency_key=rec.idempotency_key,
                    request_fingerprint=rec.request_fingerprint,
                    state="completed",
                    response_status=response_status,
                    response_body=response_body,
                    resource_id=resource_id,
                    created_at=rec.created_at,
                    expires_at=rec.expires_at,
                )
                return


class FakeRevisionRepository:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def append(
        self,
        *,
        user_id: UUID,
        moment_id: UUID,
        revision: int,
        operation: str,
        snapshot: dict,
        actor_user_id: UUID | None = None,
    ) -> None:
        self.calls.append(
            {
                "user_id": user_id,
                "moment_id": moment_id,
                "revision": revision,
                "operation": operation,
                "snapshot": snapshot,
                "actor_user_id": actor_user_id,
            }
        )


class FakeAuditRepository:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def append(
        self,
        *,
        user_id: UUID | None,
        actor_type: str,
        actor_id: str | None,
        event_type: str,
        resource_type: str | None = None,
        resource_id: UUID | None = None,
        request_id: str | None = None,
        allowed: bool,
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.calls.append({"event_type": event_type, "allowed": allowed})


class FakeSession:
    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class FakeStorage(ObjectStorage):
    """内存态对象存储，模拟 presigned URL 与 head_object。"""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], ObjectMetadata] = {}
        self._blobs: dict[tuple[str, str], bytes] = {}
        self._thumbnails: dict[tuple[str, str], bytes] = {}
        self.upload_ttl = 600
        self.download_ttl = 300

    def seed_object(
        self,
        *,
        user_id: str,
        asset_id: str,
        size: int,
        content_type: str,
        data: bytes | None = None,
    ) -> None:
        self._objects[(user_id, asset_id)] = ObjectMetadata(
            size_bytes=size,
            content_type=content_type,
            etag="fake-etag",
        )
        if data is not None:
            self._blobs[(user_id, asset_id)] = data

    def create_upload_intent(
        self,
        *,
        user_id: str,
        asset_id: str,
        content_type: str,
        size_bytes: int,
        expires_in_seconds: int,
    ) -> UploadIntent:
        return UploadIntent(
            asset_id=asset_id,
            method="PUT",
            url=f"https://fake-s3.test/upload/{user_id}/{asset_id}",
            expires_in_seconds=expires_in_seconds,
            headers={"Content-Type": content_type},
        )

    def head_object(self, *, user_id: str, asset_id: str) -> ObjectMetadata:
        meta = self._objects.get((user_id, asset_id))
        if meta is None:
            raise KeyError(f"object not found: {user_id}/{asset_id}")
        return meta

    def get_object_bytes(self, *, user_id: str, asset_id: str) -> bytes:
        try:
            return self._blobs[(user_id, asset_id)]
        except KeyError:
            raise KeyError(f"blob not found: {user_id}/{asset_id}") from None

    def put_thumbnail(
        self,
        *,
        user_id: str,
        asset_id: str,
        data: bytes,
        content_type: str,
    ) -> None:
        self._thumbnails[(user_id, asset_id)] = data

    def create_thumbnail_url(
        self,
        *,
        user_id: str,
        asset_id: str,
        expires_in_seconds: int,
    ) -> str:
        return f"https://fake-s3.test/thumbnail/{user_id}/{asset_id}?expires={expires_in_seconds}"

    def has_thumbnail(self, *, user_id: str, asset_id: str) -> bool:
        return (user_id, asset_id) in self._thumbnails

    def thumbnail_bytes(self, *, user_id: str, asset_id: str) -> bytes:
        return self._thumbnails[(user_id, asset_id)]

    def create_download_url(
        self,
        *,
        user_id: str,
        asset_id: str,
        expires_in_seconds: int,
    ) -> str:
        return f"https://fake-s3.test/download/{user_id}/{asset_id}?expires={expires_in_seconds}"


@pytest.fixture
def fake_repos() -> dict[str, Any]:
    return {
        "moment": FakeMomentRepository(),
        "confirmation": FakeConfirmationRepository(),
        "idempotency": FakeIdempotencyRepository(),
        "revision": FakeRevisionRepository(),
        "audit": FakeAuditRepository(),
        "asset": FakeAssetRepository(),
        "moment_asset": FakeMomentAssetRepository(),
    }


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def app(
    tmp_path: Path,
    fake_repos: dict[str, Any],
    fake_storage: FakeStorage,
) -> Iterator[FastAPI]:
    settings = _make_settings(tmp_path)
    application = create_application(settings)

    async def _fake_auth_context() -> deps_module.AuthContext:
        return deps_module.AuthContext(user_id=USER_ID, method="casdoor")

    async def _fake_user_id() -> UUID:
        return USER_ID

    async def _fake_session() -> FakeSession:
        return FakeSession()

    async def _fake_assets_storage() -> ObjectStorage:
        return fake_storage

    async def _fake_moments_storage() -> ObjectStorage:
        # moments 路由复用 FakeStorage，验证 thumbnailUrl/downloadUrl 签发
        return fake_storage

    # 替换路由内引用的 repository 类
    original_moment_repo = moments_routes.PostgresMomentRepository
    original_confirmation_repo = moments_routes.SqlConfirmationRepository
    original_idem_repo = moments_routes.SqlIdempotencyRepository
    original_revision_repo = moments_routes.SqlMomentRevisionRepository
    original_audit_repo = moments_routes.SqlAuditEventRepository
    original_moment_asset_repo = moments_routes.MomentAssetRepository
    original_assets_asset_repo = assets_routes.AssetRepository
    original_assets_audit_repo = assets_routes.SqlAuditEventRepository

    moments_routes.PostgresMomentRepository = lambda session: fake_repos["moment"]  # type: ignore[assignment]
    moments_routes.SqlConfirmationRepository = lambda session: fake_repos["confirmation"]  # type: ignore[assignment]
    moments_routes.SqlIdempotencyRepository = lambda session: fake_repos["idempotency"]  # type: ignore[assignment]
    moments_routes.SqlMomentRevisionRepository = lambda session: fake_repos["revision"]  # type: ignore[assignment]
    moments_routes.SqlAuditEventRepository = lambda session: fake_repos["audit"]  # type: ignore[assignment]
    moments_routes.AssetRepository = lambda session: fake_repos["asset"]  # type: ignore[assignment]
    moments_routes.MomentAssetRepository = lambda session: fake_repos["moment_asset"]  # type: ignore[assignment]
    assets_routes.AssetRepository = lambda session: fake_repos["asset"]  # type: ignore[assignment]
    assets_routes.SqlAuditEventRepository = lambda session: fake_repos["audit"]  # type: ignore[assignment]

    application.dependency_overrides[deps_module.get_auth_context] = _fake_auth_context
    application.dependency_overrides[deps_module.get_authenticated_user_id] = _fake_user_id
    application.dependency_overrides[deps_module.get_db_session] = _fake_session
    application.dependency_overrides[assets_routes.get_storage] = _fake_assets_storage
    application.dependency_overrides[moments_routes.get_storage] = _fake_moments_storage

    yield application

    # 恢复
    moments_routes.PostgresMomentRepository = original_moment_repo  # type: ignore[assignment]
    moments_routes.SqlConfirmationRepository = original_confirmation_repo  # type: ignore[assignment]
    moments_routes.SqlIdempotencyRepository = original_idem_repo  # type: ignore[assignment]
    moments_routes.SqlMomentRevisionRepository = original_revision_repo  # type: ignore[assignment]
    moments_routes.SqlAuditEventRepository = original_audit_repo  # type: ignore[assignment]
    moments_routes.MomentAssetRepository = original_moment_asset_repo  # type: ignore[assignment]
    assets_routes.AssetRepository = original_assets_asset_repo  # type: ignore[assignment]
    assets_routes.SqlAuditEventRepository = original_assets_audit_repo  # type: ignore[assignment]


# ---------- 测试 ----------


@pytest.mark.asyncio
async def test_create_upload_intent_image(
    app: FastAPI, fake_repos: dict[str, Any], fake_storage: FakeStorage
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/jpeg", "sizeBytes": 102400},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["method"] == "PUT"
    assert body["url"].startswith("https://fake-s3.test/upload/")
    assert body["headers"]["Content-Type"] == "image/jpeg"
    assert body["storageKey"].startswith(f"users/{USER_ID}/assets/")
    # 审计已记录
    assert any(c["event_type"] == "asset.upload_intent_created" for c in fake_repos["audit"].calls)


@pytest.mark.asyncio
async def test_create_upload_intent_unsupported_type(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "application/x-evil", "sizeBytes": 1024},
        )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "MEDIA_TYPE_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_create_upload_intent_too_large(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/png", "sizeBytes": 10**12},
        )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "MEDIA_TOO_LARGE"


@pytest.mark.asyncio
async def test_complete_upload_success(
    app: FastAPI, fake_repos: dict[str, Any], fake_storage: FakeStorage
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 创建 upload-intent
        intent_resp = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/jpeg", "sizeBytes": 204800},
        )
        asset_id = intent_resp.json()["assetId"]
        # 2. 模拟客户端已上传，seed head_object 元数据
        fake_storage.seed_object(
            user_id=str(USER_ID),
            asset_id=asset_id,
            size=204800,
            content_type="image/jpeg",
        )
        # 3. complete
        comp_resp = await client.post(
            f"/v1/assets/{asset_id}/complete",
            json={"checksumSha256": "abc123"},
        )
    assert comp_resp.status_code == 200
    body = comp_resp.json()
    assert body["state"] == "ready"
    assert body["sizeBytes"] == 204800
    assert body["contentType"] == "image/jpeg"
    # 审计已记录
    assert any(c["event_type"] == "asset.upload_completed" for c in fake_repos["audit"].calls)


@pytest.mark.asyncio
async def test_complete_upload_generates_thumbnail(
    app: FastAPI, fake_repos: dict[str, Any], fake_storage: FakeStorage
) -> None:
    """image 类 complete 成功后应生成缩略图并标记 thumbnail_generated_at。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        intent_resp = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/jpeg", "sizeBytes": len(PNG_1PX)},
        )
        asset_id = intent_resp.json()["assetId"]
        fake_storage.seed_object(
            user_id=str(USER_ID),
            asset_id=asset_id,
            size=len(PNG_1PX),
            content_type="image/jpeg",
            data=PNG_1PX,
        )
        comp_resp = await client.post(f"/v1/assets/{asset_id}/complete", json={})
    assert comp_resp.status_code == 200
    assert comp_resp.json()["state"] == "ready"
    # 缩略图已写入对象存储
    assert fake_storage.has_thumbnail(user_id=str(USER_ID), asset_id=asset_id)
    thumb = fake_storage.thumbnail_bytes(user_id=str(USER_ID), asset_id=asset_id)
    assert thumb[:4] == b"RIFF"  # WebP 魔数
    # 领域层已标记
    stored = await fake_repos["asset"].get_by_id(UUID(asset_id), USER_ID)
    assert stored is not None and stored.thumbnail_generated_at is not None
    # 审计已记录
    assert any(c["event_type"] == "asset.thumbnail_generated" for c in fake_repos["audit"].calls)


@pytest.mark.asyncio
async def test_complete_upload_corrupt_image_degrades(
    app: FastAPI, fake_repos: dict[str, Any], fake_storage: FakeStorage
) -> None:
    """图片损坏/不可解码时缩略图降级，不影响上传成功。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        intent_resp = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/jpeg", "sizeBytes": 512},
        )
        asset_id = intent_resp.json()["assetId"]
        fake_storage.seed_object(
            user_id=str(USER_ID),
            asset_id=asset_id,
            size=512,
            content_type="image/jpeg",
            data=b"this is not a real image",
        )
        comp_resp = await client.post(f"/v1/assets/{asset_id}/complete", json={})
    assert comp_resp.status_code == 200
    assert comp_resp.json()["state"] == "ready"
    assert not fake_storage.has_thumbnail(user_id=str(USER_ID), asset_id=asset_id)
    stored = await fake_repos["asset"].get_by_id(UUID(asset_id), USER_ID)
    assert stored is not None and stored.thumbnail_generated_at is None


@pytest.mark.asyncio
async def test_complete_upload_audio_no_thumbnail(
    app: FastAPI, fake_repos: dict[str, Any], fake_storage: FakeStorage
) -> None:
    """非 image 类不生成缩略图。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        intent_resp = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "audio/mpeg", "sizeBytes": 4096},
        )
        asset_id = intent_resp.json()["assetId"]
        fake_storage.seed_object(
            user_id=str(USER_ID),
            asset_id=asset_id,
            size=4096,
            content_type="audio/mpeg",
        )
        comp_resp = await client.post(f"/v1/assets/{asset_id}/complete", json={})
    assert comp_resp.status_code == 200
    assert not fake_storage.has_thumbnail(user_id=str(USER_ID), asset_id=asset_id)
    stored = await fake_repos["asset"].get_by_id(UUID(asset_id), USER_ID)
    assert stored is not None and stored.thumbnail_generated_at is None


@pytest.mark.asyncio
async def test_complete_upload_object_missing(
    app: FastAPI, fake_repos: dict[str, Any], fake_storage: FakeStorage
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        intent_resp = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/png", "sizeBytes": 1024},
        )
        asset_id = intent_resp.json()["assetId"]
        # 不 seed 对象，head_object 应失败
        comp_resp = await client.post(f"/v1/assets/{asset_id}/complete", json={})
    assert comp_resp.status_code == 422
    assert comp_resp.json()["error"]["code"] == "MEDIA_NOT_READY"
    # Asset 应被标记为 failed
    asset = await fake_repos["asset"].get_by_id(UUID(asset_id), USER_ID)
    assert asset is not None
    assert asset.state == AssetState.FAILED


@pytest.mark.asyncio
async def test_complete_upload_idempotent(app: FastAPI, fake_storage: FakeStorage) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        intent_resp = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/jpeg", "sizeBytes": 1024},
        )
        asset_id = intent_resp.json()["assetId"]
        fake_storage.seed_object(
            user_id=str(USER_ID), asset_id=asset_id, size=1024, content_type="image/jpeg"
        )
        first = await client.post(f"/v1/assets/{asset_id}/complete", json={})
        second = await client.post(f"/v1/assets/{asset_id}/complete", json={})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()


@pytest.mark.asyncio
async def test_complete_upload_not_found(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/v1/assets/{uuid4()}/complete", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ASSET_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_asset_metadata(app: FastAPI, fake_storage: FakeStorage) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        intent_resp = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "audio/mpeg", "sizeBytes": 5120},
        )
        asset_id = intent_resp.json()["assetId"]
        resp = await client.get(f"/v1/assets/{asset_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "uploading"
    assert body["kind"] == "audio"
    assert body["contentType"] == "audio/mpeg"
    assert body["readyAt"] is None


@pytest.mark.asyncio
async def test_get_asset_not_found(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/v1/assets/{uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ASSET_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_asset_invalid_uuid(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/assets/not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_download_url_success(app: FastAPI, fake_storage: FakeStorage) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        intent_resp = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/jpeg", "sizeBytes": 1024},
        )
        asset_id = intent_resp.json()["assetId"]
        fake_storage.seed_object(
            user_id=str(USER_ID), asset_id=asset_id, size=1024, content_type="image/jpeg"
        )
        await client.post(f"/v1/assets/{asset_id}/complete", json={})
        resp = await client.post(f"/v1/assets/{asset_id}/download-url")
    assert resp.status_code == 200
    body = resp.json()
    assert body["url"].startswith("https://fake-s3.test/download/")
    assert body["expiresIn"] > 0
    assert body["expiresAt"]


@pytest.mark.asyncio
async def test_download_url_not_ready(app: FastAPI, fake_storage: FakeStorage) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        intent_resp = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/jpeg", "sizeBytes": 1024},
        )
        asset_id = intent_resp.json()["assetId"]
        # 不 complete，state 仍为 uploading
        resp = await client.post(f"/v1/assets/{asset_id}/download-url")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "MEDIA_NOT_READY"


@pytest.mark.asyncio
async def test_download_url_not_found(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/v1/assets/{uuid4()}/download-url")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ASSET_NOT_FOUND"


@pytest.mark.asyncio
async def test_create_moment_with_asset_ids(
    app: FastAPI, fake_repos: dict[str, Any], fake_storage: FakeStorage
) -> None:
    """创建 Moment 时携带 assetIds，应建立关联并在响应中返回 media 数组。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 上传两张图片
        intent1 = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/jpeg", "sizeBytes": 1024},
        )
        asset1 = intent1.json()["assetId"]
        intent2 = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/png", "sizeBytes": 2048},
        )
        asset2 = intent2.json()["assetId"]
        # 2. complete 两张
        for aid in (asset1, asset2):
            fake_storage.seed_object(
                user_id=str(USER_ID), asset_id=aid, size=1024, content_type="image/jpeg"
            )
            await client.post(f"/v1/assets/{aid}/complete", json={})
        # 3. 创建 Moment 引用 assetIds
        moment_resp = await client.post(
            "/v1/moments",
            json={"title": "带图的 Moment", "assetIds": [asset1, asset2]},
            headers={"Idempotency-Key": "asset-moment-001"},
        )
    assert moment_resp.status_code == 201
    body = moment_resp.json()
    # media 数组按 assetIds 顺序返回
    assert "media" in body
    assert len(body["media"]) == 2
    assert body["media"][0]["assetId"] == asset1
    assert body["media"][1]["assetId"] == asset2
    assert body["media"][0]["type"] == "image/jpeg"
    assert body["media"][0]["thumbnailUrl"] is None
    # 关联已建立
    moment_id = UUID(body["id"])
    links = await fake_repos["moment_asset"].list_by_moment(moment_id, USER_ID)
    assert len(links) == 2
    assert links[0].position == 0
    assert links[1].position == 1


@pytest.mark.asyncio
async def test_moment_media_thumbnail_url_signed_when_available(
    app: FastAPI, fake_repos: dict[str, Any], fake_storage: FakeStorage
) -> None:
    """有缩略图的 asset 在 media 响应中签发 thumbnailUrl；无缩略图的保持 null。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # asset1：真实图片 → 生成缩略图；asset2：无数据 → 降级无缩略图
        intent1 = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/jpeg", "sizeBytes": len(PNG_1PX)},
        )
        asset1 = intent1.json()["assetId"]
        intent2 = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/png", "sizeBytes": 2048},
        )
        asset2 = intent2.json()["assetId"]
        fake_storage.seed_object(
            user_id=str(USER_ID),
            asset_id=asset1,
            size=len(PNG_1PX),
            content_type="image/jpeg",
            data=PNG_1PX,
        )
        fake_storage.seed_object(
            user_id=str(USER_ID), asset_id=asset2, size=2048, content_type="image/png"
        )
        for aid in (asset1, asset2):
            await client.post(f"/v1/assets/{aid}/complete", json={})
        moment_resp = await client.post(
            "/v1/moments",
            json={"title": "带缩略图 Moment", "assetIds": [asset1, asset2]},
            headers={"Idempotency-Key": "thumb-moment-001"},
        )
        # 详情接口同样签发
        detail_resp = await client.get(f"/v1/moments/{moment_resp.json()['id']}")
    assert moment_resp.status_code == 201
    body = moment_resp.json()
    assert body["media"][0]["assetId"] == asset1
    assert body["media"][0]["thumbnailUrl"].startswith("https://fake-s3.test/thumbnail/")
    assert body["media"][1]["assetId"] == asset2
    assert body["media"][1]["thumbnailUrl"] is None
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["media"][0]["thumbnailUrl"].startswith("https://fake-s3.test/thumbnail/")
    assert detail["media"][0]["downloadUrl"].startswith("https://fake-s3.test/download/")


@pytest.mark.asyncio
async def test_create_moment_with_nonexistent_asset(
    app: FastAPI, fake_repos: dict[str, Any]
) -> None:
    """assetIds 包含不存在的 ID 时应返回 404。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/moments",
            json={"title": "坏 Asset", "assetIds": [str(uuid4())]},
            headers={"Idempotency-Key": "bad-asset-001"},
        )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ASSET_NOT_FOUND"


@pytest.mark.asyncio
async def test_create_moment_with_not_ready_asset(app: FastAPI, fake_storage: FakeStorage) -> None:
    """assetIds 包含未 complete 的 Asset 时应返回 409。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        intent = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/jpeg", "sizeBytes": 1024},
        )
        asset_id = intent.json()["assetId"]
        # 不 complete，state=uploading
        resp = await client.post(
            "/v1/moments",
            json={"title": "未就绪 Asset", "assetIds": [asset_id]},
            headers={"Idempotency-Key": "not-ready-001"},
        )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "MEDIA_NOT_READY"


@pytest.mark.asyncio
async def test_get_moment_includes_media(
    app: FastAPI, fake_repos: dict[str, Any], fake_storage: FakeStorage
) -> None:
    """GET /v1/moments/{id} 响应应包含 media 数组。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        intent = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/jpeg", "sizeBytes": 1024},
        )
        asset_id = intent.json()["assetId"]
        fake_storage.seed_object(
            user_id=str(USER_ID), asset_id=asset_id, size=1024, content_type="image/jpeg"
        )
        await client.post(f"/v1/assets/{asset_id}/complete", json={})
        create_resp = await client.post(
            "/v1/moments",
            json={"title": "查询 media", "assetIds": [asset_id]},
            headers={"Idempotency-Key": "get-media-001"},
        )
        moment_id = create_resp.json()["id"]
        resp = await client.get(f"/v1/moments/{moment_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "media" in body
    assert len(body["media"]) == 1
    assert body["media"][0]["assetId"] == asset_id


@pytest.mark.asyncio
async def test_list_moments_includes_media(app: FastAPI, fake_storage: FakeStorage) -> None:
    """GET /v1/moments 列表响应应包含 media 数组。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        intent = await client.post(
            "/v1/assets/upload-intents",
            json={"contentType": "image/jpeg", "sizeBytes": 1024},
        )
        asset_id = intent.json()["assetId"]
        fake_storage.seed_object(
            user_id=str(USER_ID), asset_id=asset_id, size=1024, content_type="image/jpeg"
        )
        await client.post(f"/v1/assets/{asset_id}/complete", json={})
        await client.post(
            "/v1/moments",
            json={"title": "列表 media", "assetIds": [asset_id]},
            headers={"Idempotency-Key": "list-media-001"},
        )
        resp = await client.get("/v1/moments")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert len(body["items"]) >= 1
    item = body["items"][0]
    assert "media" in item
    assert len(item["media"]) == 1
    assert item["media"][0]["assetId"] == asset_id
