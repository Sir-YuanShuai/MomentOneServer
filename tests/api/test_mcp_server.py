"""MCP Server API 测试（Streamable HTTP 全链路）。

驱动 JSON-RPC over ASGI（无需真实 DB/Casdoor）：
- 无 token → 401 + WWW-Authenticate
- QR Binding token（认证双形态之一）→ initialize → tools/list → tools/call
- bookkeeping_create 幂等/类型校验/审计、bookkeeping_list/summary、moments_get
- 缺 moments.write → SCOPE_DENIED
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.api.routes import moments as moments_routes
from app.application import create_application
from app.core.config import Settings
from app.infrastructure.database.repositories.idempotency_repository import (
    IdempotencyRecord,
)
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.mcp import tools as mcp_tools
from app.modules.mcp.endpoint import McpComponent
from app.modules.mcp.token_verifier import MomentTokenVerifier
from app.modules.mcp_oauth.service import derive_code_challenge, generate_code_verifier
from app.modules.moments.domain import Moment
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
BINDING_ID = UUID("22222222-2222-4222-8222-222222222222")
DEVICE_ID = "device-aaa-bbb-ccc"
PROTOCOL_VERSION = "2025-06-18"

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": PROTOCOL_VERSION,
}


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
    apps_html = tmp_path / "bookkeeping.html"
    apps_html.write_text(
        "<!DOCTYPE html><html><body><div id=app>test bookkeeping app</div>"
        '<script src="https://example.invalid/app.js"></script></body></html>',
        encoding="utf-8",
    )
    return Settings(
        env="test",
        database_url="postgresql+psycopg://test:test@127.0.0.1:5432/test",
        jwt_private_key_path=str(priv_path),
        jwt_public_key_path=str(pub_path),
        jwt_issuer="https://momentone.test",
        jwt_audience="momentone-glasses",
        access_token_ttl_seconds=3600,
        refresh_token_ttl_seconds=90 * 24 * 3600,
        binding_code_ttl_seconds=300,
        binding_code_length=24,
        mcp_base_url="http://testserver",
        mcp_apps_html_path=str(apps_html),
    )


class _FakeActive:
    """active 状态记录（binding 或 mcp_authorization 通用）。"""

    status = "active"
    last_active_at: object = None
    updated_at: object = None


class FakeBindingSession:
    """验证 token 时返回 active 绑定的假 session（binding + mcp_authorization 通用）。"""

    async def __aenter__(self) -> FakeBindingSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, stmt: object) -> FakeResult:
        return FakeResult()

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class FakeResult:
    def scalar_one_or_none(self) -> _FakeActive:
        return _FakeActive()


@contextlib.asynccontextmanager
async def _binding_session_factory() -> AsyncGenerator[FakeBindingSession]:
    async with FakeBindingSession() as session:
        yield session


class FakeSession:
    """MCP 工具执行用假 session（repositories 被 monkeypatch 后不真正使用）。"""

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


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
        user_id: UUID,
        *,
        limit: int = 20,
        cursor: str | None = None,
        moment_type: str | None = None,
        category: str | None = None,
        tag: str | None = None,
        goal_id: UUID | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        payload_eq: dict[str, str] | None = None,
    ) -> tuple[list[Moment], bool, str | None]:
        items = [m for m in self._store.values() if m.user_id == user_id]
        if moment_type:
            items = [m for m in items if m.moment_type == moment_type]
        if category:
            items = [m for m in items if m.category.value == category]
        if occurred_from:
            items = [m for m in items if m.occurred_at >= occurred_from]
        if occurred_to:
            items = [m for m in items if m.occurred_at <= occurred_to]
        if payload_eq:
            for key, value in payload_eq.items():
                items = [m for m in items if str(m.payload.get(key)) == value]
        items.sort(key=lambda m: (m.occurred_at, m.id), reverse=True)
        return items[:limit], len(items) > limit, None

    async def list_by_user_range(
        self,
        user_id: UUID,
        *,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        moment_type: str | None = None,
        payload_eq: dict[str, str] | None = None,
    ) -> list[Moment]:
        items = [m for m in self._store.values() if m.user_id == user_id]
        if moment_type:
            items = [m for m in items if m.moment_type == moment_type]
        if occurred_from:
            items = [m for m in items if m.occurred_at >= occurred_from]
        if occurred_to:
            items = [m for m in items if m.occurred_at <= occurred_to]
        if payload_eq:
            for key, value in payload_eq.items():
                items = [m for m in items if str(m.payload.get(key)) == value]
        return items


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
        if existing is not None:
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
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + ttl,
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
        for rec in self._store.values():
            if rec.id == record_id:
                updated = IdempotencyRecord(
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
                self._store[(rec.user_id, rec.operation, rec.idempotency_key)] = updated
                return


class FakeAuditRepository:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def append(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class FakeRevisionRepository:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def append(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def fake_repos() -> dict[str, Any]:
    return {
        "moment": FakeMomentRepository(),
        "idempotency": FakeIdempotencyRepository(),
        "audit": FakeAuditRepository(),
        "revision": FakeRevisionRepository(),
    }


@pytest.fixture
def app(
    tmp_path: Path,
    fake_repos: dict[str, Any],
) -> Iterator[FastAPI]:
    settings = _make_settings(tmp_path)

    verifier = MomentTokenVerifier(
        settings,
        session_factory=lambda: _binding_session_factory(),  # type: ignore[arg-type]
    )
    component = McpComponent(settings, verifier=verifier)
    application = create_application(settings, mcp_component=component)

    # monkeypatch tools 模块内的 repository 类
    original = {
        name: getattr(mcp_tools, name)
        for name in (
            "PostgresMomentRepository",
            "SqlIdempotencyRepository",
            "SqlAuditEventRepository",
            "SqlMomentRevisionRepository",
        )
    }
    mcp_tools.PostgresMomentRepository = lambda session: fake_repos["moment"]  # type: ignore[assignment]
    mcp_tools.SqlIdempotencyRepository = lambda session: fake_repos["idempotency"]  # type: ignore[assignment]
    mcp_tools.SqlAuditEventRepository = lambda session: fake_repos["audit"]  # type: ignore[assignment]
    mcp_tools.SqlMomentRevisionRepository = lambda session: fake_repos["revision"]  # type: ignore[assignment]

    # 发现端点用测试 settings（mcp_base_url=testserver）
    from app.core.config import get_settings

    application.dependency_overrides[get_settings] = lambda: settings

    yield application

    for name, cls in original.items():
        setattr(mcp_tools, name, cls)


@contextlib.asynccontextmanager
async def _mcp_client(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    """进入 lifespan（启动 MCP session manager）+ ASGI 客户端。"""
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
            yield client


def _issue_glasses_token(settings: Settings, *, scope: tuple[str, ...]) -> str:
    issuer = JwtIssuer(settings)
    token, _ = issuer.issue_access_token(
        binding_id=BINDING_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        scope=scope,
    )
    return token


def _issue_mcp_token(
    settings: Settings, *, scope: tuple[str, ...], client_id: str = "test-client"
) -> str:
    issuer = JwtIssuer(settings)
    token, _ = issuer.issue_mcp_access_token(
        user_id=USER_ID,
        scope=scope,
        client_id=client_id,
        resource="http://testserver/mcp",
    )
    return token


async def _initialize(client: AsyncClient, token: str) -> str:
    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test-host", "version": "1.0"},
            },
        },
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.headers.get("mcp-session-id")
    assert session_id
    # notifications/initialized
    resp2 = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}", "Mcp-Session-Id": session_id},
    )
    assert resp2.status_code in (200, 202), resp2.text
    return session_id


async def _post(
    client: AsyncClient,
    session_id: str,
    token: str,
    payload: dict,
) -> dict:
    resp = await client.post(
        "/mcp",
        json=payload,
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}", "Mcp-Session-Id": session_id},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    message = data[0] if isinstance(data, list) else data
    assert message.get("jsonrpc") == "2.0", message
    return message


@pytest.mark.asyncio
async def test_mcp_requires_bearer_token(app: FastAPI) -> None:
    async with _mcp_client(app) as client:
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers=MCP_HEADERS,
        )
    assert resp.status_code == 401
    www = resp.headers.get("www-authenticate", "")
    assert "Bearer" in www
    assert "resource_metadata" in www


@pytest.mark.asyncio
async def test_mcp_list_tools_with_qr_binding_token(
    app: FastAPI, fake_repos: dict[str, Any], tmp_path: Path
) -> None:
    """认证双形态之一：QR Binding token 可经 MCP 端点识别用户并列出工具。"""
    settings = _make_settings(tmp_path)
    token = _issue_glasses_token(settings, scope=("moments.read", "moments.write"))

    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)
        data = await _post(
            client,
            session_id,
            token,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
    tools = data["result"]["tools"]
    names = [t["name"] for t in tools]
    assert {"bookkeeping_create", "bookkeeping_list", "bookkeeping_summary", "moments_get"} <= set(
        names
    )
    create = next(t for t in tools if t["name"] == "bookkeeping_create")
    assert "inputSchema" in create
    assert "moments.write" in create.get("description", "")
    list_tool = next(t for t in tools if t["name"] == "bookkeeping_list")
    assert (
        list_tool.get("_meta", {}).get("ui", {}).get("resourceUri") == "ui://moment-one/bookkeeping"
    )


@pytest.mark.asyncio
async def test_bookkeeping_create_flow(
    app: FastAPI, fake_repos: dict[str, Any], tmp_path: Path
) -> None:
    """bookkeeping_create：创建成功 + provenance(source=mcp) + 审计 actorType=mcp + 幂等。"""
    settings = _make_settings(tmp_path)
    token = _issue_glasses_token(settings, scope=("moments.read", "moments.write"))

    # 单 client 会话内完成两次调用（session manager 每个 app 只 run 一次）
    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)
        data = await _post(
            client,
            session_id,
            token,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "bookkeeping_create",
                    "arguments": {
                        "amount": 35.5,
                        "flow": "expense",
                        "occurredAt": "2026-08-05T12:30:00+08:00",
                        "category": "餐饮",
                        "merchant": "楼下便利店",
                        "idempotencyKey": "bk-key-1",
                    },
                },
            },
        )
        assert "error" not in data, data
        result = data["result"]
        assert result["isError"] in (False, None)
        sc = result["structuredContent"]
        assert sc["amount"] == 35.5
        assert sc["flow"] == "expense"
        assert sc["revision"] == 1

        # 落库 + 幂等缓存 + 审计
        assert len(fake_repos["moment"]._store) == 1
        audit = fake_repos["audit"].calls
        assert any(c["actor_type"] == "mcp" for c in audit)
        assert any(c["event_type"] == "mcp.tool.bookkeeping_create" for c in audit)

        # 相同 idempotencyKey 再调 → 返回缓存，不重复创建
        data2 = await _post(
            client,
            session_id,
            token,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "bookkeeping_create",
                    "arguments": {
                        "amount": 35.5,
                        "flow": "expense",
                        "occurredAt": "2026-08-05T12:30:00+08:00",
                        "category": "餐饮",
                        "merchant": "楼下便利店",
                        "idempotencyKey": "bk-key-1",
                    },
                },
            },
        )
    assert len(fake_repos["moment"]._store) == 1
    assert data2["result"]["structuredContent"]["id"] == sc["id"]


@pytest.mark.asyncio
async def test_bookkeeping_create_invalid_payload(app: FastAPI, tmp_path: Path) -> None:
    """非法 payload（occurredAt 格式错误）→ INVALID_ARGUMENTS（领域校验）。"""
    settings = _make_settings(tmp_path)
    token = _issue_glasses_token(settings, scope=("moments.read", "moments.write"))

    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)
        data = await _post(
            client,
            session_id,
            token,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "bookkeeping_create",
                    "arguments": {
                        "amount": 10,
                        "flow": "expense",
                        "occurredAt": "not-a-date",
                    },
                },
            },
        )
    result = data["result"]
    assert result["isError"] is True
    err = result["structuredContent"]["error"]
    assert err["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_bookkeeping_create_signature_rejection(app: FastAPI, tmp_path: Path) -> None:
    """签名层校验（amount 负数 / flow 非法枚举）→ isError 文本错误。"""
    settings = _make_settings(tmp_path)
    token = _issue_glasses_token(settings, scope=("moments.read", "moments.write"))

    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)
        data = await _post(
            client,
            session_id,
            token,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "bookkeeping_create",
                    "arguments": {
                        "amount": -5,
                        "flow": "transfer",
                        "occurredAt": "2026-08-05T12:30:00+08:00",
                    },
                },
            },
        )
    result = data["result"]
    assert result["isError"] is True
    text = "".join(c.get("text", "") for c in result["content"])
    assert "amount" in text


@pytest.mark.asyncio
async def test_bookkeeping_create_scope_denied(app: FastAPI, tmp_path: Path) -> None:
    """只有 moments.read 的 token 调写工具 → SCOPE_DENIED。"""
    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read",))

    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)
        data = await _post(
            client,
            session_id,
            token,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "bookkeeping_create",
                    "arguments": {
                        "amount": 10,
                        "flow": "expense",
                        "occurredAt": "2026-08-05T12:30:00+08:00",
                    },
                },
            },
        )
    assert data["result"]["isError"] is True
    assert data["result"]["structuredContent"]["error"]["code"] == "SCOPE_DENIED"


@pytest.mark.asyncio
async def test_bookkeeping_list_and_summary(
    app: FastAPI, fake_repos: dict[str, Any], tmp_path: Path
) -> None:
    """先造两条账（收入 + 支出），list 过滤 + summary 聚合（口径与 Web 一致）。"""
    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read", "moments.write"))

    repo = fake_repos["moment"]
    now = datetime.now(UTC)

    def seed(amount: float, flow: str, category: str, occurred: datetime) -> None:
        m = Moment(
            id=uuid4(),
            user_id=USER_ID,
            title=category,
            description=None,
            voice_input=None,
            ai_summary=None,
            category=moments_routes.MomentCategory.EXPERIENCE,
            tags=(),
            persons=(),
            event=None,
            occurred_at=occurred,
            timezone="UTC",
            revision=1,
            created_at=now,
            updated_at=now,
            provenance=None,
            moment_type="bookkeeping",
            payload={"amount": amount, "flow": flow, "category": category},
        )
        repo._store[m.id] = m

    seed(35.5, "expense", "餐饮", now - timedelta(days=1))
    seed(5000, "income", "工资", now - timedelta(days=2))
    seed(12.0, "expense", "交通", now - timedelta(days=3))

    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)
        list_data = await _post(
            client,
            session_id,
            token,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "bookkeeping_list", "arguments": {"limit": 20}},
            },
        )
        sum_data = await _post(
            client,
            session_id,
            token,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "bookkeeping_summary", "arguments": {"period": "month"}},
            },
        )

    list_sc = list_data["result"]["structuredContent"]
    assert list_sc["total"] == 3
    assert list_sc["items"][0]["flow"] == "expense"  # 时间倒序

    sum_sc = sum_data["result"]["structuredContent"]
    assert sum_sc["income"] == 5000
    assert sum_sc["expense"] == 47.5
    assert sum_sc["balance"] == 4952.5
    assert sum_sc["count"] == 3
    cats = {c["category"]: c["amount"] for c in sum_sc["byCategory"]}
    assert cats["餐饮"] == 35.5
    assert cats["交通"] == 12.0
    assert "工资" not in cats  # 收入不进分类小计


@pytest.mark.asyncio
async def test_moments_get_not_found(app: FastAPI, tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read",))

    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)
        data = await _post(
            client,
            session_id,
            token,
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "moments_get",
                    "arguments": {"momentId": str(uuid4())},
                },
            },
        )
    assert data["result"]["isError"] is True
    assert data["result"]["structuredContent"]["error"]["code"] == "MOMENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_discovery_endpoints(app: FastAPI) -> None:
    """RFC 9728 / RFC 8414 发现端点（根路径 + /mcp 子路径）。"""
    async with _mcp_client(app) as client:
        pr = await client.get("/.well-known/oauth-protected-resource")
        pr_mcp = await client.get("/.well-known/oauth-protected-resource/mcp")
        asm = await client.get("/.well-known/oauth-authorization-server")
        asm_mcp = await client.get("/.well-known/oauth-authorization-server/mcp")

    assert pr.status_code == 200
    assert pr.json()["resource"] == "http://testserver/mcp"
    assert pr.json()["authorization_servers"] == ["http://testserver"]
    assert pr_mcp.json() == pr.json()

    meta = asm.json()
    assert meta["issuer"] == "http://testserver"
    assert meta["authorization_endpoint"] == "http://testserver/oauth/authorize"
    assert meta["token_endpoint"] == "http://testserver/oauth/token"
    assert meta["registration_endpoint"] == "http://testserver/oauth/register"
    assert "S256" in meta["code_challenge_methods_supported"]
    assert "moments.read" in meta["scopes_supported"]
    assert asm_mcp.json() == meta


@pytest.mark.asyncio
async def test_pkce_helper() -> None:
    """PKCE S256 challenge 可复验（token 交换测试依赖）。"""
    verifier = generate_code_verifier()
    challenge = derive_code_challenge(verifier)
    assert challenge == derive_code_challenge(verifier)
    assert "=" not in challenge
