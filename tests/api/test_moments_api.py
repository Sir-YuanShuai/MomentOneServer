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
from app.application import create_application
from app.core.config import Settings
from app.infrastructure.database.repositories.confirmation_repository import (
    PendingConfirmation,
)
from app.infrastructure.database.repositories.idempotency_repository import (
    IdempotencyRecord,
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

    async def list_by_user(
        self, *, user_id: UUID, limit: int, cursor: str | None
    ) -> tuple[list[Moment], bool, str | None]:
        items = [m for m in self._store.values() if m.user_id == user_id]
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


class FakeSession:
    """空 AsyncSession，repositories 不实际使用 session。"""

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


@pytest.fixture
def fake_repos() -> dict[str, Any]:
    return {
        "moment": FakeMomentRepository(),
        "confirmation": FakeConfirmationRepository(),
        "idempotency": FakeIdempotencyRepository(),
        "revision": FakeRevisionRepository(),
        "audit": FakeAuditRepository(),
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

    # 替换路由内引用的 repository 类
    original_moment_repo = moments_routes.PostgresMomentRepository
    original_confirmation_repo = moments_routes.SqlConfirmationRepository
    original_idem_repo = moments_routes.SqlIdempotencyRepository
    original_revision_repo = moments_routes.SqlMomentRevisionRepository
    original_audit_repo = moments_routes.SqlAuditEventRepository

    moments_routes.PostgresMomentRepository = lambda session: fake_repos["moment"]  # type: ignore[assignment]
    moments_routes.SqlConfirmationRepository = lambda session: fake_repos["confirmation"]  # type: ignore[assignment]
    moments_routes.SqlIdempotencyRepository = lambda session: fake_repos["idempotency"]  # type: ignore[assignment]
    moments_routes.SqlMomentRevisionRepository = lambda session: fake_repos["revision"]  # type: ignore[assignment]
    moments_routes.SqlAuditEventRepository = lambda session: fake_repos["audit"]  # type: ignore[assignment]

    application.dependency_overrides[deps_module.get_auth_context] = _fake_auth_context
    application.dependency_overrides[deps_module.get_authenticated_user_id] = _fake_user_id
    application.dependency_overrides[deps_module.get_db_session] = _fake_session

    yield application

    # 恢复
    moments_routes.PostgresMomentRepository = original_moment_repo  # type: ignore[assignment]
    moments_routes.SqlConfirmationRepository = original_confirmation_repo  # type: ignore[assignment]
    moments_routes.SqlIdempotencyRepository = original_idem_repo  # type: ignore[assignment]
    moments_routes.SqlMomentRevisionRepository = original_revision_repo  # type: ignore[assignment]
    moments_routes.SqlAuditEventRepository = original_audit_repo  # type: ignore[assignment]


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
