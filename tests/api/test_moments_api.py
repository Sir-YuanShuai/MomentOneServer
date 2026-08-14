"""Moments 路由 API 测试。

通过 dependency_overrides 注入 Fake AuthContext 和内存态 Repository，
不依赖数据库和 Casdoor。覆盖：
- POST /v1/moments 创建（含 provenance 推断 + 幂等去重）
- GET /v1/moments 列表
- GET /v1/moments/{id} 详情
- PATCH /v1/moments/{id} 更新（含 revision conflict）
- POST /v1/moments/{id}/delete-preview + delete-confirm 两阶段删除
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.api import deps as deps_module
from app.api.routes import moments as moments_routes
from app.api.routes import sync as sync_routes
from app.application import create_application
from app.core.config import Settings
from app.infrastructure.database.repositories.confirmation_repository import (
    PendingConfirmation,
)
from app.infrastructure.database.repositories.idempotency_repository import (
    IdempotencyRecord,
)
from app.modules.assets.domain import Asset as AssetDomain
from app.modules.assets.domain import (
    AssetKind,
    AssetState,
    MomentAssetLink,
    build_storage_key,
)
from app.modules.moments.domain import (
    Moment,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
DEVICE_ID = "device-aaa-bbb-ccc"


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


class FakeMomentRepository:
    """内存态 Moment Repository。"""

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

    async def get_by_id_including_deleted(self, moment_id: UUID, user_id: UUID) -> Moment | None:
        m = self._store.get(moment_id)
        return m if m is not None and m.user_id == user_id else None

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
        # 简化：直接替换字段
        updated = Moment(
            id=m.id,
            user_id=m.user_id,
            title=fields.get("title", m.title),
            description=fields.get("description", m.description),
            voice_input=fields.get("voice_input", m.voice_input),
            ai_summary=fields.get("ai_summary", m.ai_summary),
            category=fields.get("category", m.category),
            tags=fields.get("tags", m.tags),
            persons=fields.get("persons", m.persons),
            event=fields.get("event", m.event),
            occurred_at=fields.get("occurred_at", m.occurred_at),
            timezone=fields.get("timezone", m.timezone),
            revision=m.revision + 1,
            created_at=m.created_at,
            updated_at=datetime.now(UTC),
            location=fields.get("location", m.location),
            emotion=fields.get("emotion", m.emotion),
            provenance=m.provenance,
            deleted_at=m.deleted_at,
            moment_type=fields.get("moment_type", m.moment_type),
            payload=fields.get("payload", m.payload),
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
            moment_type=m.moment_type,
            payload=m.payload,
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
        # dataclass frozen，重建
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


class FakeSyncChangeRepository:
    def __init__(self) -> None:
        self._items: list[Any] = []

    async def append(
        self,
        *,
        user_id: UUID,
        entity_id: UUID,
        operation: str,
        revision: int,
        snapshot: dict,
    ) -> Any:
        item = type(
            "SyncChange",
            (),
            {
                "sequence": len(self._items) + 1,
                "user_id": user_id,
                "entity_type": "moment",
                "entity_id": entity_id,
                "operation": operation,
                "revision": revision,
                "snapshot": snapshot,
            },
        )()
        self._items.append(item)
        return item

    async def list_after(self, *, user_id: UUID, sequence: int, limit: int) -> list[Any]:
        return [
            item for item in self._items if item.user_id == user_id and item.sequence > sequence
        ][:limit]

    async def latest_sequence(self, *, user_id: UUID) -> int:
        items = [item.sequence for item in self._items if item.user_id == user_id]
        return max(items, default=0)


class FakeAssetRepository:
    """内存态 Asset Repository。"""

    def __init__(self) -> None:
        self._store: dict[UUID, Any] = {}

    async def create(
        self, *, user_id: UUID, kind: Any, content_type: str, size_bytes: int | None = None
    ) -> Any:
        asset_id = uuid4()
        self._store[asset_id] = AssetDomain(
            id=asset_id,
            user_id=user_id,
            state=AssetState.UPLOADING,
            kind=AssetKind(kind) if isinstance(kind, str) else kind,
            storage_key=build_storage_key(user_id, asset_id),
            content_type=content_type,
            size_bytes=size_bytes,
            checksum_sha256=None,
            created_at=datetime.now(UTC),
            ready_at=None,
            thumbnail_generated_at=None,
            deleted_at=None,
        )
        return self._store[asset_id]

    async def get_by_id(self, asset_id: UUID, user_id: UUID) -> Any | None:
        a = self._store.get(asset_id)
        if a is None or a.user_id != user_id or a.deleted_at is not None:
            return None
        return a

    async def mark_ready(
        self, asset_id: UUID, user_id: UUID, *, size_bytes: int, checksum_sha256: str | None = None
    ) -> Any | None:
        a = self._store.get(asset_id)
        if a is None or a.user_id != user_id:
            return None
        self._store[asset_id] = AssetDomain(
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

    async def mark_failed(self, asset_id: UUID, user_id: UUID) -> Any | None:
        a = self._store.get(asset_id)
        if a is None or a.user_id != user_id:
            return None
        self._store[asset_id] = AssetDomain(
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
    """内存态 MomentAsset 关联 Repository。"""

    def __init__(self) -> None:
        self._links: list[Any] = []

    async def attach(
        self, *, user_id: UUID, moment_id: UUID, asset_id: UUID, position: int, role: Any = None
    ) -> Any:
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

    async def list_by_moment(self, moment_id: UUID, user_id: UUID) -> list[Any]:
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


class FakeSession:
    """空 AsyncSession，repositories 不实际使用 session。"""

    async def flush(self) -> None:
        pass

    def begin_nested(self) -> "FakeSession":
        return self

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class FakeHabitGoalRepository:
    """内存态 HabitGoal Repository（供 payload.goalId 归属校验）。"""

    def __init__(self) -> None:
        self._store: dict[UUID, UUID] = {}  # goal_id -> user_id

    def seed(self, goal_id: UUID, user_id: UUID) -> None:
        self._store[goal_id] = user_id

    async def get_by_id(self, goal_id: UUID, user_id: UUID) -> Any | None:
        if self._store.get(goal_id) != user_id:
            return None
        return {"id": goal_id, "user_id": user_id, "name": "test-goal"}


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
        "habit_goal": FakeHabitGoalRepository(),
        "sync_change": FakeSyncChangeRepository(),
    }


@pytest.fixture
def app(tmp_path: Path, fake_repos: dict[str, Any]) -> Iterator[FastAPI]:
    settings = _make_settings(tmp_path)
    application = create_application(settings)

    async def _fake_auth_context() -> deps_module.AuthContext:
        return deps_module.AuthContext(user_id=USER_ID, method="casdoor")

    async def _fake_user_id() -> UUID:
        return USER_ID

    async def _fake_session() -> FakeSession:
        return FakeSession()

    async def _fake_storage() -> None:
        # 测试环境不接 S3，media 字段省略 downloadUrl/thumbnailUrl
        return None

    # 替换路由内引用的 repository 类
    original_moment_repo = moments_routes.PostgresMomentRepository
    original_confirmation_repo = moments_routes.SqlConfirmationRepository
    original_idem_repo = moments_routes.SqlIdempotencyRepository
    original_revision_repo = moments_routes.SqlMomentRevisionRepository
    original_audit_repo = moments_routes.SqlAuditEventRepository
    original_asset_repo = moments_routes.AssetRepository
    original_moment_asset_repo = moments_routes.MomentAssetRepository
    original_habit_goal_repo = moments_routes.SqlHabitGoalRepository
    original_sync_idem_repo = sync_routes.SqlIdempotencyRepository
    original_sync_change_repo = sync_routes.SqlSyncChangeRepository

    moments_routes.PostgresMomentRepository = lambda session: fake_repos["moment"]  # type: ignore[assignment]
    moments_routes.SqlConfirmationRepository = lambda session: fake_repos["confirmation"]  # type: ignore[assignment]
    moments_routes.SqlIdempotencyRepository = lambda session: fake_repos["idempotency"]  # type: ignore[assignment]
    moments_routes.SqlMomentRevisionRepository = lambda session: fake_repos["revision"]  # type: ignore[assignment]
    moments_routes.SqlAuditEventRepository = lambda session: fake_repos["audit"]  # type: ignore[assignment]
    moments_routes.AssetRepository = lambda session: fake_repos["asset"]  # type: ignore[assignment]
    moments_routes.MomentAssetRepository = lambda session: fake_repos["moment_asset"]  # type: ignore[assignment]
    moments_routes.SqlHabitGoalRepository = lambda session: fake_repos["habit_goal"]  # type: ignore[assignment]
    sync_routes.SqlIdempotencyRepository = lambda session: fake_repos["idempotency"]  # type: ignore[assignment]
    sync_routes.SqlSyncChangeRepository = lambda session: fake_repos["sync_change"]  # type: ignore[assignment]

    application.dependency_overrides[deps_module.get_auth_context] = _fake_auth_context
    application.dependency_overrides[deps_module.get_authenticated_user_id] = _fake_user_id
    application.dependency_overrides[deps_module.get_db_session] = _fake_session
    application.dependency_overrides[moments_routes.get_storage] = _fake_storage

    yield application

    # 恢复
    moments_routes.PostgresMomentRepository = original_moment_repo  # type: ignore[assignment]
    moments_routes.SqlConfirmationRepository = original_confirmation_repo  # type: ignore[assignment]
    moments_routes.SqlIdempotencyRepository = original_idem_repo  # type: ignore[assignment]
    moments_routes.SqlMomentRevisionRepository = original_revision_repo  # type: ignore[assignment]
    moments_routes.SqlAuditEventRepository = original_audit_repo  # type: ignore[assignment]
    moments_routes.AssetRepository = original_asset_repo  # type: ignore[assignment]
    moments_routes.MomentAssetRepository = original_moment_asset_repo  # type: ignore[assignment]
    moments_routes.SqlHabitGoalRepository = original_habit_goal_repo  # type: ignore[assignment]
    sync_routes.SqlIdempotencyRepository = original_sync_idem_repo  # type: ignore[assignment]
    sync_routes.SqlSyncChangeRepository = original_sync_change_repo  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_create_moment_with_provenance(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/moments",
            json={"title": "测试 Moment", "description": "hello"},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "测试 Moment"
    assert body["revision"] == 1
    # casdoor 路径推断 provenance.source = web
    assert body["provenance"]["source"] == "web"
    # 版本快照已记录
    rev_calls = fake_repos["revision"].calls
    assert len(rev_calls) == 1
    assert rev_calls[0]["operation"] == "created"
    # 审计已记录
    audit_calls = fake_repos["audit"].calls
    assert any(c["event_type"] == "moment.created" for c in audit_calls)


@pytest.mark.asyncio
async def test_create_moment_idempotency(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    payload = {"title": "幂等测试", "description": "same"}
    headers = {"Idempotency-Key": "key-001"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp1 = await client.post("/v1/moments", json=payload, headers=headers)
        resp2 = await client.post("/v1/moments", json=payload, headers=headers)
    assert resp1.status_code == 201
    assert resp2.status_code == 201
    # 第二次返回缓存响应，不创建新 Moment
    assert resp1.json()["id"] == resp2.json()["id"]


@pytest.mark.asyncio
async def test_create_moment_from_openai_files_is_unattended_and_idempotent(
    app: FastAPI, fake_repos: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import_calls: list[list[str]] = []

    async def _fake_import(refs: list[Any], **kwargs: Any) -> list[str]:
        import_calls.append([ref.id for ref in refs])
        asset = await fake_repos["asset"].create(
            user_id=USER_ID,
            kind=AssetKind.IMAGE,
            content_type="image/jpeg",
            size_bytes=4,
        )
        await fake_repos["asset"].mark_ready(
            asset.id, USER_ID, size_bytes=4, checksum_sha256="0" * 64
        )
        return [str(asset.id)]

    monkeypatch.setattr(moments_routes, "_import_openai_files", _fake_import)
    payload: dict[str, Any] = {
        "title": "带图记录",
        "openaiFileIdRefs": [
            {
                "name": "photo.jpg",
                "id": "file-stable-123",
                "mime_type": "image/jpeg",
                "download_link": "https://files.oaiusercontent.com/temporary-one",
            }
        ],
    }
    headers = {"Idempotency-Key": "openai-file-key-001"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/v1/moments/from-openai-files", json=payload, headers=headers)
        payload["openaiFileIdRefs"][0]["download_link"] = (
            "https://files.oaiusercontent.com/temporary-two"
        )
        replay = await client.post("/v1/moments/from-openai-files", json=payload, headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]
    assert len(first.json()["media"]) == 1
    assert import_calls == [["file-stable-123"]]


@pytest.mark.asyncio
async def test_create_moment_idempotency_conflict(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp1 = await client.post(
            "/v1/moments",
            json={"title": "第一版"},
            headers={"Idempotency-Key": "key-002"},
        )
        resp2 = await client.post(
            "/v1/moments",
            json={"title": "不同内容"},
            headers={"Idempotency-Key": "key-002"},
        )
    assert resp1.status_code == 201
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_update_moment_idempotency(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    headers = {"Idempotency-Key": "update-key-001"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/v1/moments", json={"title": "更新幂等测试"})
        moment_id = created.json()["id"]
        payload = {"expectedRevision": 1, "title": "只更新一次"}
        first = await client.patch(f"/v1/moments/{moment_id}", json=payload, headers=headers)
        replay = await client.patch(f"/v1/moments/{moment_id}", json=payload, headers=headers)
    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json()["revision"] == 2
    assert replay.json()["revision"] == 2
    assert fake_repos["moment"]._store[UUID(moment_id)].revision == 2


@pytest.mark.asyncio
async def test_update_moment_idempotency_conflict(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    headers = {"Idempotency-Key": "update-key-002"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/v1/moments", json={"title": "更新冲突测试"})
        moment_id = created.json()["id"]
        first = await client.patch(
            f"/v1/moments/{moment_id}",
            json={"expectedRevision": 1, "title": "版本一"},
            headers=headers,
        )
        conflict = await client.patch(
            f"/v1/moments/{moment_id}",
            json={"expectedRevision": 1, "title": "版本二"},
            headers=headers,
        )
    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_create_moment_accepts_client_generated_id(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    moment_id = uuid4()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/moments",
            json={"id": str(moment_id), "title": "离线创建"},
            headers={"Idempotency-Key": "offline-create-id"},
        )
    assert response.status_code == 201
    assert response.json()["id"] == str(moment_id)


@pytest.mark.asyncio
async def test_sync_operations_replays_and_allows_partial_success(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    created_id = uuid4()
    missing_id = uuid4()
    create_operation = {
        "operationId": str(uuid4()),
        "kind": "moment.create",
        "idempotencyKey": "offline-sync-create-1",
        "entityId": str(created_id),
        "payload": {
            "id": str(created_id),
            "title": "离线记录",
            "timezone": "Asia/Shanghai",
        },
    }
    conflict_operation = {
        "operationId": str(uuid4()),
        "kind": "moment.update",
        "idempotencyKey": "offline-sync-update-1",
        "entityId": str(missing_id),
        "expectedRevision": 1,
        "payload": {"title": "不会写入"},
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/sync/operations",
            json={"operations": [conflict_operation, create_operation]},
        )
        replay = await client.post(
            "/v1/sync/operations",
            json={"operations": [create_operation]},
        )
    assert response.status_code == 200
    results = {item["operationId"]: item for item in response.json()["results"]}
    assert results[conflict_operation["operationId"]]["status"] == "rejected"
    assert results[create_operation["operationId"]]["status"] == "applied"
    assert results[create_operation["operationId"]]["entity"]["id"] == str(created_id)
    assert replay.status_code == 200
    assert replay.json()["results"][0]["entity"]["id"] == str(created_id)


@pytest.mark.asyncio
async def test_sync_changes_uses_server_cursor_and_returns_tombstone(
    app: FastAPI, fake_repos: dict[str, Any]
) -> None:
    moment_id = uuid4()
    await fake_repos["sync_change"].append(
        user_id=USER_ID,
        entity_id=moment_id,
        operation="upsert",
        revision=1,
        snapshot={"id": str(moment_id), "revision": 1, "deletedAt": None},
    )
    await fake_repos["sync_change"].append(
        user_id=USER_ID,
        entity_id=moment_id,
        operation="delete",
        revision=2,
        snapshot={"id": str(moment_id), "revision": 2, "deletedAt": "2026-08-12T00:00:00Z"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        baseline = await client.get("/v1/sync/changes")
        changes = await client.get("/v1/sync/changes?cursor=1")
    assert baseline.status_code == 200
    assert [item["operation"] for item in baseline.json()["changes"]] == ["upsert", "delete"]
    assert baseline.json()["nextCursor"] == "2"
    assert changes.status_code == 200
    body = changes.json()
    assert body["nextCursor"] == "2"
    assert body["changes"][0]["operation"] == "delete"
    assert body["changes"][0]["entity"]["deletedAt"] is not None


@pytest.mark.asyncio
async def test_get_moment(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    # 先创建
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/v1/moments", json={"title": "查询测试"})
        moment_id = create_resp.json()["id"]
        resp = await client.get(f"/v1/moments/{moment_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == moment_id


@pytest.mark.asyncio
async def test_get_moment_not_found(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/v1/moments/{uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MOMENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_update_moment_revision_conflict(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/v1/moments", json={"title": "冲突测试"})
        moment_id = create_resp.json()["id"]
        # 传错误的 expectedRevision
        resp = await client.patch(
            f"/v1/moments/{moment_id}",
            json={"expectedRevision": 999, "title": "新标题"},
        )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REVISION_CONFLICT"


@pytest.mark.asyncio
async def test_update_moment_success(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/v1/moments", json={"title": "更新测试"})
        moment_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/v1/moments/{moment_id}",
            json={"expectedRevision": 1, "title": "更新后标题"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "更新后标题"
    assert body["revision"] == 2
    # provenance 不可篡改：更新后仍保留原值
    assert body["provenance"]["source"] == "web"
    # 版本快照记录 updated
    rev_calls = fake_repos["revision"].calls
    assert any(c["operation"] == "updated" for c in rev_calls)


@pytest.mark.asyncio
async def test_two_phase_delete(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/v1/moments", json={"title": "删除测试"})
        moment_id = create_resp.json()["id"]
        revision = create_resp.json()["revision"]

        # 预览
        preview_resp = await client.post(
            f"/v1/moments/{moment_id}/delete-preview",
            json={"expectedRevision": revision},
        )
        assert preview_resp.status_code == 200
        confirmation_id = preview_resp.json()["confirmationId"]

        # 确认
        confirm_resp = await client.post(
            "/v1/moments/delete-confirm",
            json={"confirmationId": confirmation_id},
        )
        assert confirm_resp.status_code == 204

        # 再查应 404（已软删除，get_by_id 不返回已删除）
        get_resp = await client.get(f"/v1/moments/{moment_id}")
    assert get_resp.status_code == 404
    # 版本快照记录 deleted
    rev_calls = fake_repos["revision"].calls
    assert any(c["operation"] == "deleted" for c in rev_calls)
    # 审计记录 moment.deleted
    audit_calls = fake_repos["audit"].calls
    assert any(c["event_type"] == "moment.deleted" for c in audit_calls)


@pytest.mark.asyncio
async def test_delete_confirm_without_preview(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/moments/delete-confirm",
            json={"confirmationId": str(uuid4())},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "CONFIRMATION_REQUIRED"


@pytest.mark.asyncio
async def test_delete_confirm_reuse(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/v1/moments", json={"title": "重复确认"})
        moment_id = create_resp.json()["id"]
        revision = create_resp.json()["revision"]

        preview_resp = await client.post(
            f"/v1/moments/{moment_id}/delete-preview",
            json={"expectedRevision": revision},
        )
        confirmation_id = preview_resp.json()["confirmationId"]

        # 第一次确认成功
        confirm1 = await client.post(
            "/v1/moments/delete-confirm",
            json={"confirmationId": confirmation_id},
        )
        assert confirm1.status_code == 204

        # 第二次确认应失败
        confirm2 = await client.post(
            "/v1/moments/delete-confirm",
            json={"confirmationId": confirmation_id},
        )
    assert confirm2.status_code == 400
    assert confirm2.json()["error"]["code"] == "CONFIRMATION_USED"


# ---- 内置记录类型（type + payload）----


@pytest.mark.asyncio
async def test_create_bookkeeping_moment(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    payload_body = {"amount": 38.5, "flow": "expense", "account": "微信", "category": "餐饮"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/moments",
            json={"title": "午餐", "type": "bookkeeping", "payload": payload_body},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["type"] == "bookkeeping"
    assert body["payload"] == payload_body


@pytest.mark.asyncio
async def test_create_habit_moment(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    payload_body = {"habit": "晨跑", "done": True, "unit": "公里", "count": 5}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/moments",
            json={"title": "晨跑打卡", "type": "habit", "payload": payload_body},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["type"] == "habit"
    assert body["payload"] == payload_body


@pytest.mark.asyncio
async def test_create_moment_defaults_to_general(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/moments", json={"title": "自由记录"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["type"] == "general"
    assert body["payload"] == {}


@pytest.mark.asyncio
async def test_create_moment_unknown_type(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/moments",
            json={"title": "未知类型", "type": "travel", "payload": {}},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_create_moment_invalid_payload(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # bookkeeping 缺必填 flow
        resp = await client.post(
            "/v1/moments",
            json={"title": "记账", "type": "bookkeeping", "payload": {"amount": 10}},
        )
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["code"] == "INVALID_ARGUMENTS"
    assert body["details"]["type"] == "bookkeeping"


@pytest.mark.asyncio
async def test_create_general_with_payload_rejected(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/moments",
            json={"title": "记账", "type": "general", "payload": {"amount": 10}},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_list_moments_type_filter(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/moments",
            json={
                "title": "记账1",
                "type": "bookkeeping",
                "payload": {"amount": 10, "flow": "expense"},
            },
        )
        await client.post(
            "/v1/moments",
            json={
                "title": "记账2",
                "type": "bookkeeping",
                "payload": {"amount": 20, "flow": "income"},
            },
        )
        await client.post(
            "/v1/moments",
            json={"title": "打卡", "type": "habit", "payload": {"habit": "阅读", "done": True}},
        )
        await client.post("/v1/moments", json={"title": "自由"})

        resp = await client.get("/v1/moments", params={"type": "bookkeeping", "limit": 100})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 2
        assert all(item["type"] == "bookkeeping" for item in items)

        resp_habit = await client.get("/v1/moments", params={"type": "habit", "limit": 100})
    assert resp_habit.status_code == 200
    assert len(resp_habit.json()["items"]) == 1
    assert resp_habit.json()["items"][0]["type"] == "habit"


@pytest.mark.asyncio
async def test_update_moment_type_payload(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/v1/moments", json={"title": "自由记录"})
        moment_id = create_resp.json()["id"]

        resp = await client.patch(
            f"/v1/moments/{moment_id}",
            json={
                "expectedRevision": 1,
                "type": "bookkeeping",
                "payload": {"amount": 66, "flow": "expense", "account": "现金"},
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "bookkeeping"
    assert body["payload"] == {"amount": 66, "flow": "expense", "account": "现金"}
    assert body["revision"] == 2
    # provenance 不可篡改
    assert body["provenance"]["source"] == "web"


@pytest.mark.asyncio
async def test_update_moment_invalid_payload(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/moments",
            json={
                "title": "记账",
                "type": "bookkeeping",
                "payload": {"amount": 10, "flow": "expense"},
            },
        )
        moment_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/v1/moments/{moment_id}",
            json={"expectedRevision": 1, "payload": {"amount": -5, "flow": "expense"}},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_list_moments_category_tag_filter(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/moments",
            json={"title": "旅行", "category": "travel", "tags": ["西湖"]},
        )
        await client.post(
            "/v1/moments",
            json={"title": "美食", "category": "food", "tags": ["面馆"]},
        )
        resp_cat = await client.get("/v1/moments", params={"category": "travel", "limit": 100})
        assert len(resp_cat.json()["items"]) == 1
        assert resp_cat.json()["items"][0]["category"] == "travel"

        resp_tag = await client.get("/v1/moments", params={"tag": "面馆", "limit": 100})
        assert len(resp_tag.json()["items"]) == 1
        assert resp_tag.json()["items"][0]["title"] == "美食"


# ---- habit 打卡关联习惯目标（payload.goalId）----


@pytest.mark.asyncio
async def test_create_habit_with_goal_ref(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    goal_id = uuid4()
    fake_repos["habit_goal"].seed(goal_id, USER_ID)
    transport = ASGITransport(app=app)
    payload_body = {"habit": "游泳", "done": True, "goalId": str(goal_id)}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/moments",
            json={"title": "游泳打卡", "type": "habit", "payload": payload_body},
        )
    assert resp.status_code == 201
    assert resp.json()["payload"] == payload_body


@pytest.mark.asyncio
async def test_create_habit_with_unknown_goal_ref(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/moments",
            json={
                "title": "游泳打卡",
                "type": "habit",
                "payload": {"habit": "游泳", "done": True, "goalId": str(uuid4())},
            },
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"
    assert "goalId" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_create_habit_with_invalid_goal_id_format(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/moments",
            json={
                "title": "游泳打卡",
                "type": "habit",
                "payload": {"habit": "游泳", "done": True, "goalId": "not-a-uuid"},
            },
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


# ---- 通用描述维度（persons / event，ADR-0019）----


@pytest.mark.asyncio
async def test_create_moment_with_persons_and_event(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/moments",
            json={
                "title": "家庭聚餐",
                "persons": ["妈妈", "小王"],
                "event": "家庭聚餐",
                "tags": ["聚餐"],
            },
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["persons"] == ["妈妈", "小王"]
    assert body["event"] == "家庭聚餐"


@pytest.mark.asyncio
async def test_update_moment_persons_and_event(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = (await client.post("/v1/moments", json={"title": "普通记录"})).json()
        resp = await client.patch(
            f"/v1/moments/{created['id']}",
            json={"expectedRevision": 1, "persons": ["同事"], "event": "项目复盘"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["persons"] == ["同事"]
    assert body["event"] == "项目复盘"
    assert body["revision"] == 2


@pytest.mark.asyncio
async def test_create_moment_with_persons_item_too_long(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/moments",
            json={"title": "超长人物", "persons": ["x" * 21]},
        )
    assert resp.status_code == 422  # Pydantic 校验拒绝


@pytest.mark.asyncio
async def test_batch_delete_uses_one_preview_and_one_confirm(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = (await client.post("/v1/moments", json={"title": "第一条"})).json()
        second = (await client.post("/v1/moments", json={"title": "第二条"})).json()
        preview = await client.post(
            "/v1/moments/batch-delete-preview",
            json={
                "items": [
                    {"id": first["id"], "expectedRevision": first["revision"]},
                    {"id": second["id"], "expectedRevision": second["revision"]},
                ]
            },
        )
        confirmed = await client.post(
            "/v1/moments/batch-delete-confirm",
            json={"confirmationId": preview.json()["confirmationId"]},
        )
    assert preview.status_code == 200
    assert preview.json()["count"] == 2
    assert confirmed.status_code == 200
    assert confirmed.json()["deletedCount"] == 2
