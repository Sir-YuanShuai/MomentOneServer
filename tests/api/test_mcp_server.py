"""MCP Server API 测试（Streamable HTTP 全链路）。

驱动 JSON-RPC over ASGI（无需真实 DB/Casdoor）：
- 无 token → 401 + WWW-Authenticate
- QR Binding token（认证双形态之一）→ initialize → tools/list → tools/call
- bookkeeping_create 幂等/类型校验/审计、bookkeeping_list/summary、moments_get
- 缺 moments.write → SCOPE_DENIED
"""

from __future__ import annotations

import contextlib
import json
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
from app.modules.habit_goals.domain import HabitGoal
from app.modules.mcp import tools as mcp_tools
from app.modules.mcp.a2ui import (
    A2UI_CATALOG_ID,
    A2UI_MIME_TYPE,
    A2UI_VERSION,
    validate_a2ui_messages,
)
from app.modules.mcp.deps import McpToolEnv
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
A2UI_CAPABILITIES = {
    "experimental": {
        "a2ui": {
            "clientCapabilities": {
                A2UI_VERSION: {
                    "supportedCatalogIds": [A2UI_CATALOG_ID],
                    "inlineCatalogs": [],
                }
            }
        }
    }
}

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

    def __init__(self, scope: str | list[str]) -> None:
        self.status = "active"
        # 模拟 ORM 的 ARRAY 列：存为 list（token_verifier 读取 binding.scope）
        self.scope = scope.split() if isinstance(scope, str) else list(scope)
        self.last_active_at: object = None
        self.updated_at: object = None


class FakeBindingSession:
    """验证 token 时返回 active 绑定/授权的假 session（binding + authorization 通用）。

    - scope：device_bindings.scope（legacy 镜像）与 mcp_authorizations.scope
      （统一授权记录，权限事实源）默认一致；
    - authorization_scope：可单独指定授权记录 scope（Web 端调整后实时生效场景）；
      None 表示无授权记录（存量绑定回退 device_bindings.scope 场景）。
    """

    def __init__(
        self,
        scope: str = "moments.read moments.write",
        authorization_scope: str | None | object = ...,
    ) -> None:
        self._scope = scope
        self._authorization_scope = scope if authorization_scope is ... else authorization_scope

    async def __aenter__(self) -> FakeBindingSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, stmt: object) -> FakeResult:
        from app.infrastructure.database.models import DeviceBinding as DeviceBindingORM
        from app.infrastructure.database.models import McpAuthorization

        entity = None
        try:
            descriptions = stmt.column_descriptions  # type: ignore[attr-defined]
            if descriptions:
                entity = descriptions[0].get("entity")
        except Exception:
            entity = None
        if entity is DeviceBindingORM:
            return FakeResult(self._scope)
        if entity is McpAuthorization:
            return FakeResult(self._authorization_scope)  # type: ignore[arg-type]
        return FakeResult(self._scope)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class FakeResult:
    def __init__(self, scope: str | None) -> None:
        self._scope = scope

    def scalar_one_or_none(self) -> _FakeActive | None:
        if self._scope is None:
            return None
        return _FakeActive(self._scope)


# 测试用 mcp_authorizations.scope（权限以授权记录为准）
FAKE_AUTH_SCOPE = "moments.read moments.write"


@contextlib.asynccontextmanager
async def _binding_session_factory() -> AsyncGenerator[FakeBindingSession]:
    async with FakeBindingSession(FAKE_AUTH_SCOPE) as session:
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


class FakeHabitGoalRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, HabitGoal] = {}

    async def create(self, goal: HabitGoal) -> HabitGoal:
        self._store[goal.id] = goal
        return goal

    async def get_by_id(self, goal_id: UUID, user_id: UUID) -> HabitGoal | None:
        goal = self._store.get(goal_id)
        if goal is None or goal.user_id != user_id or goal.deleted_at is not None:
            return None
        return goal

    async def list_by_user(self, user_id: UUID) -> list[HabitGoal]:
        return [
            goal
            for goal in self._store.values()
            if goal.user_id == user_id and goal.deleted_at is None
        ]


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
        "habit": FakeHabitGoalRepository(),
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
    component = McpComponent(settings, verifier=verifier, env=McpToolEnv(enforce_quotas=False))
    application = create_application(settings, mcp_component=component)

    # monkeypatch tools 模块内的 repository 类
    original = {
        name: getattr(mcp_tools, name)
        for name in (
            "PostgresMomentRepository",
            "SqlIdempotencyRepository",
            "SqlAuditEventRepository",
            "SqlMomentRevisionRepository",
            "SqlHabitGoalRepository",
        )
    }
    mcp_tools.PostgresMomentRepository = lambda session: fake_repos["moment"]  # type: ignore[assignment]
    mcp_tools.SqlIdempotencyRepository = lambda session: fake_repos["idempotency"]  # type: ignore[assignment]
    mcp_tools.SqlAuditEventRepository = lambda session: fake_repos["audit"]  # type: ignore[assignment]
    mcp_tools.SqlMomentRevisionRepository = lambda session: fake_repos["revision"]  # type: ignore[assignment]
    mcp_tools.SqlHabitGoalRepository = lambda session: fake_repos["habit"]  # type: ignore[assignment]

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


async def _initialize_with_result(
    client: AsyncClient,
    token: str,
    *,
    capabilities: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": capabilities or {},
                "clientInfo": {"name": "test-host", "version": "1.0"},
            },
        },
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.headers.get("mcp-session-id")
    assert session_id
    payload = resp.json()
    message = payload[0] if isinstance(payload, list) else payload
    assert "result" in message, message
    resp2 = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}", "Mcp-Session-Id": session_id},
    )
    assert resp2.status_code in (200, 202), resp2.text
    return session_id, message["result"]


async def _initialize(
    client: AsyncClient, token: str, *, capabilities: dict[str, Any] | None = None
) -> str:
    session_id, _ = await _initialize_with_result(client, token, capabilities=capabilities)
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
    assert {
        "bookkeeping_create",
        "bookkeeping_list",
        "bookkeeping_summary",
        "moments_create",
        "moments_list",
        "moments_search",
        "moments_count",
        "reviews_daily",
        "moments_get",
        "habit_goals_list",
        "habit_goal_create",
        "habit_checkin_create",
        "habit_progress",
        "agent_plan",
        "a2ui_action",
    } <= set(names)
    create = next(t for t in tools if t["name"] == "bookkeeping_create")
    assert "inputSchema" in create
    assert "moments.write" in create.get("description", "")
    list_tool = next(t for t in tools if t["name"] == "bookkeeping_list")
    assert (
        list_tool.get("_meta", {}).get("ui", {}).get("resourceUri") == "ui://moment-one/bookkeeping"
    )
    timeline_tool = next(t for t in tools if t["name"] == "moments_list")
    assert timeline_tool.get("_meta", {}).get("ui", {}).get("resourceUri") == (
        "ui://moment-one/timeline"
    )
    habit_tool = next(t for t in tools if t["name"] == "habit_progress")
    assert habit_tool.get("_meta", {}).get("ui", {}).get("resourceUri") == (
        "ui://moment-one/habits"
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
    """授权记录只有 moments.read 时（权限以授权记录为准），写工具 → SCOPE_DENIED。"""
    import sys

    mod = sys.modules[__name__]  # type: ignore[assignment]  # pytest 实际模块

    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read", "moments.write"))

    original_scope = mod.FAKE_AUTH_SCOPE  # type: ignore[attr-defined]
    mod.FAKE_AUTH_SCOPE = "moments.read"  # type: ignore[attr-defined]  # 授权记录只有读
    try:
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
    finally:
        mod.FAKE_AUTH_SCOPE = original_scope  # type: ignore[attr-defined]
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
async def test_timeline_apps_tools_flow(
    app: FastAPI, fake_repos: dict[str, Any], tmp_path: Path
) -> None:
    """时间线 App：创建 → 列表 → 搜索 → 每日回顾 → 数量统计。"""
    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read", "moments.write"))

    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)

        async def call(tool: str, arguments: dict[str, Any], request_id: int) -> dict:
            data = await _post(
                client,
                session_id,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
            )
            assert data["result"].get("isError") in (False, None), data
            return data["result"]["structuredContent"]

        created = await call(
            "moments_create",
            {
                "title": "西湖边的晚风",
                "description": "和朋友沿着湖边散步",
                "category": "travel",
                "tags": ["杭州", "散步"],
                "occurredAt": datetime.now(UTC).isoformat(),
                "timezone": "Asia/Shanghai",
                "idempotencyKey": "moment-app-key-001",
            },
            30,
        )
        moment_id = created["moment"]["id"]
        assert created["created"] is True
        assert created["moment"]["provenance"]["source"] == "mcp"

        timeline = await call("moments_list", {"limit": 20}, 31)
        assert timeline["view"] == "timeline"
        assert timeline["items"][0]["id"] == moment_id

        search = await call("moments_search", {"query": "西湖", "limit": 20}, 32)
        assert search["total"] == 1
        assert search["items"][0]["title"] == "西湖边的晚风"

        review = await call(
            "reviews_daily",
            {
                "date": datetime.now(UTC).date().isoformat(),
                "timezone": "UTC",
            },
            33,
        )
        assert review["view"] == "daily-review"
        assert review["count"] == 1

        count = await call("moments_count", {}, 34)
        assert count["count"] == 1
        assert count["byCategory"]["travel"] == 1

    assert any(
        item["event_type"] == "mcp.tool.moments_search" for item in fake_repos["audit"].calls
    )


@pytest.mark.asyncio
async def test_habit_apps_tools_flow(
    app: FastAPI, fake_repos: dict[str, Any], tmp_path: Path
) -> None:
    """习惯 App：创建目标 → 七日面板 → 打卡 → 今日状态和连续天数更新。"""
    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read", "moments.write"))

    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)

        async def call(tool: str, arguments: dict[str, Any], request_id: int) -> dict:
            data = await _post(
                client,
                session_id,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
            )
            assert data["result"].get("isError") in (False, None), data
            return data["result"]["structuredContent"]

        created = await call(
            "habit_goal_create",
            {
                "name": "晨跑",
                "frequency": "daily",
                "unit": "公里",
                "color": "#557a62",
                "idempotencyKey": "habit-goal-key-001",
            },
            40,
        )
        goal_id = created["goal"]["id"]

        before = await call("habit_progress", {"days": 7, "timezone": "UTC"}, 41)
        assert before["goals"][0]["todayDone"] is False

        checkin = await call(
            "habit_checkin_create",
            {
                "goalId": goal_id,
                "done": True,
                "count": 3,
                "timezone": "UTC",
                "idempotencyKey": "habit-checkin-key-001",
            },
            42,
        )
        assert checkin["checkin"]["type"] == "habit"

        after = await call("habit_progress", {"days": 7, "timezone": "UTC"}, 43)
        assert after["goals"][0]["todayDone"] is True
        assert after["goals"][0]["currentStreak"] == 1
        assert after["goals"][0]["completedDays"] == 1

    assert len(fake_repos["habit"]._store) == 1
    assert len(fake_repos["moment"]._store) == 1


@pytest.mark.asyncio
async def test_mcp_apps_resources_include_timeline_and_habits(app: FastAPI, tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read",))
    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)
        data = await _post(
            client,
            session_id,
            token,
            {"jsonrpc": "2.0", "id": 50, "method": "resources/list", "params": {}},
        )
    uris = {resource["uri"] for resource in data["result"]["resources"]}
    assert {
        "ui://moment-one/bookkeeping",
        "ui://moment-one/timeline",
        "ui://moment-one/habits",
    } <= uris


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


@pytest.mark.asyncio
async def test_glasses_token_legacy_colon_scope(app: FastAPI, tmp_path: Path) -> None:
    """历史冒号 scope（moments:read）兼容：绑定记录与 token 均为冒号命名时，
    经规范化后 MCP 工具可正常执行（旧设备无需重新绑定）。"""
    import sys

    mod = sys.modules[__name__]  # type: ignore[assignment]

    settings = _make_settings(tmp_path)
    issuer = JwtIssuer(settings)
    token, _ = issuer.issue_access_token(
        binding_id=BINDING_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        scope=("moments:read", "moments:write"),
    )

    original_scope = mod.FAKE_AUTH_SCOPE  # type: ignore[attr-defined]
    mod.FAKE_AUTH_SCOPE = "moments:read moments:write"  # type: ignore[attr-defined]
    try:
        async with _mcp_client(app) as client:
            session_id = await _initialize(client, token)
            created = await _post(
                client,
                session_id,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "bookkeeping_create",
                        "arguments": {
                            "amount": 9.9,
                            "flow": "expense",
                            "occurredAt": "2026-08-06T12:00:00+08:00",
                        },
                    },
                },
            )
            summary = await _post(
                client,
                session_id,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "bookkeeping_summary", "arguments": {"period": "month"}},
                },
            )
            data = await _post(
                client,
                session_id,
                token,
                {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
            )
    finally:
        mod.FAKE_AUTH_SCOPE = original_scope  # type: ignore[attr-defined]

    names = [t["name"] for t in data["result"]["tools"]]
    assert "bookkeeping_create" in names
    assert "error" not in summary, summary
    assert summary["result"]["structuredContent"]["expense"] == 9.9
    assert "error" not in created, created
    assert created["result"]["structuredContent"]["amount"] == 9.9


@pytest.mark.asyncio
async def test_glasses_authorization_scope_is_authoritative(app: FastAPI, tmp_path: Path) -> None:
    """统一授权模型：scope 以 mcp_authorizations 为准而非 token/绑定快照。

    token 与绑定记录都是完整读写，但统一授权记录只剩读 → 写工具 SCOPE_DENIED、
    读工具可用——Web 端在统一授权列表收窄权限后下一次调用即实时生效（无需重绑）。
    """
    import contextlib

    settings = _make_settings(tmp_path)
    issuer = JwtIssuer(settings)
    token, _ = issuer.issue_access_token(
        binding_id=BINDING_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        scope=("moments.read", "moments.write"),
    )

    @contextlib.asynccontextmanager
    async def _narrowed_session_factory() -> AsyncGenerator[FakeBindingSession]:
        # 绑定记录完整读写，但统一授权记录（mcp_authorizations）只有读
        async with FakeBindingSession(
            scope="moments.read moments.write", authorization_scope="moments.read"
        ) as session:
            yield session

    verifier = MomentTokenVerifier(
        settings,
        session_factory=lambda: _narrowed_session_factory(),  # type: ignore[arg-type]
    )
    component = McpComponent(settings, verifier=verifier, env=McpToolEnv(enforce_quotas=False))
    application = create_application(settings, mcp_component=component)

    async with _mcp_client(application) as client:
        session_id = await _initialize(client, token)
        read_ok = await _post(
            client,
            session_id,
            token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "bookkeeping_summary", "arguments": {"period": "month"}},
            },
        )
        write_denied = await _post(
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
                        "amount": 10,
                        "flow": "expense",
                        "occurredAt": "2026-08-06T12:00:00+08:00",
                    },
                },
            },
        )

    assert "error" not in read_ok, read_ok
    assert write_denied["result"]["isError"] is True
    assert write_denied["result"]["structuredContent"]["error"]["code"] == "SCOPE_DENIED"


@pytest.mark.asyncio
async def test_glasses_legacy_fallback_to_binding_scope(app: FastAPI, tmp_path: Path) -> None:
    """存量兼容：无统一授权记录时回退 device_bindings.scope（迁移前数据）。"""
    import contextlib

    settings = _make_settings(tmp_path)
    issuer = JwtIssuer(settings)
    token, _ = issuer.issue_access_token(
        binding_id=BINDING_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        scope=("moments.read", "moments.write"),
    )

    @contextlib.asynccontextmanager
    async def _legacy_session_factory() -> AsyncGenerator[FakeBindingSession]:
        # 绑定记录有完整读写，统一授权记录不存在（authorization_scope=None）
        async with FakeBindingSession(
            scope="moments.read moments.write", authorization_scope=None
        ) as session:
            yield session

    verifier = MomentTokenVerifier(
        settings,
        session_factory=lambda: _legacy_session_factory(),  # type: ignore[arg-type]
    )
    component = McpComponent(settings, verifier=verifier, env=McpToolEnv(enforce_quotas=False))
    application = create_application(settings, mcp_component=component)

    async with _mcp_client(application) as client:
        session_id = await _initialize(client, token)
        created = await _post(
            client,
            session_id,
            token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "bookkeeping_create",
                    "arguments": {
                        "amount": 7.7,
                        "flow": "expense",
                        "occurredAt": "2026-08-06T12:00:00+08:00",
                    },
                },
            },
        )

    assert "error" not in created, created
    assert created["result"]["structuredContent"]["amount"] == 7.7


@pytest.mark.asyncio
async def test_bookkeeping_prompt_served(app: FastAPI, tmp_path: Path) -> None:
    """远程提示词：prompts/list 可见 bookkeeping-assistant，prompts/get 返回记账指令。"""
    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read", "moments.write"))

    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)
        listed = await _post(
            client,
            session_id,
            token,
            {"jsonrpc": "2.0", "id": 2, "method": "prompts/list", "params": {}},
        )
        fetched = await _post(
            client,
            session_id,
            token,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "prompts/get",
                "params": {"name": "bookkeeping-assistant"},
            },
        )

    prompts = listed["result"]["prompts"]
    assert any(p["name"] == "bookkeeping-assistant" for p in prompts)
    messages = fetched["result"]["messages"]
    text = "".join(m["content"].get("text", "") for m in messages if m.get("content"))
    assert "bookkeeping_create" in text
    assert "bookkeeping_summary" in text
    assert "上个月" in text


@pytest.mark.asyncio
async def test_bookkeeping_plan_resolves_actions(app: FastAPI, tmp_path: Path) -> None:
    """bookkeeping_plan：上月→精确 year/month；记一笔→create 参数；非记账→none。"""
    from datetime import UTC, datetime, timedelta

    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read", "moments.write"))

    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)

        async def _plan(input_text: str) -> dict:
            resp = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {"name": "bookkeeping_plan", "arguments": {"input": input_text}},
                },
                headers={
                    **MCP_HEADERS,
                    "Authorization": f"Bearer {token}",
                    "Mcp-Session-Id": session_id,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            message = data[0] if isinstance(data, list) else data
            return message["result"]["structuredContent"]

        summary = await _plan("上个月花了多少钱")
        now = datetime.now(UTC)
        expected_month = now.month - 1 if now.month > 1 else 12
        expected_year = now.year if now.month > 1 else now.year - 1
        assert summary["action"] == "summary"
        assert summary["args"] == {
            "period": "month",
            "year": expected_year,
            "month": expected_month,
        }

        create = await _plan("记一笔午餐28.5元")
        assert create["action"] == "create"
        assert create["args"]["amount"] == 28.5
        assert create["args"]["flow"] == "expense"
        assert create["args"]["category"] == "餐饮"
        assert create["args"]["idempotencyKey"]

        create2 = await _plan("昨天打车花了20")
        assert create2["action"] == "create"
        assert create2["args"]["category"] == "交通"
        assert datetime.fromisoformat(create2["args"]["occurredAt"]) >= datetime.now(
            UTC
        ) - timedelta(days=1, hours=1)

        listed = await _plan("3月账单")
        assert listed["action"] == "list"

        none = await _plan("今天天气不错")
        assert none["action"] == "none"
        assert none["reply"]


@pytest.mark.asyncio
async def test_bookkeeping_plan_today_and_summary_custom_range(
    app: FastAPI, tmp_path: Path
) -> None:
    """plan「今天/昨天」→ summary custom from/to；summary 支持自定义范围统计。"""
    from datetime import UTC, datetime

    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read", "moments.write"))

    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)

        async def _call(name: str, arguments: dict) -> dict:
            resp = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                headers={
                    **MCP_HEADERS,
                    "Authorization": f"Bearer {token}",
                    "Mcp-Session-Id": session_id,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            message = data[0] if isinstance(data, list) else data
            return message["result"]["structuredContent"]

        today_plan = await _call("bookkeeping_plan", {"input": "今天花了多少钱"})
        assert today_plan["action"] == "summary"
        assert today_plan["args"]["period"] == "custom"
        assert today_plan["args"]["from_"] and today_plan["args"]["to"]
        now = datetime.now(UTC)
        assert (
            today_plan["args"]["from_"]
            == datetime(now.year, now.month, now.day, tzinfo=UTC).isoformat()
        )

        yesterday_plan = await _call("bookkeeping_plan", {"input": "昨天花了多少"})
        assert yesterday_plan["action"] == "summary"
        assert yesterday_plan["args"]["period"] == "custom"

        # summary 自定义范围：先造今天一笔，范围统计命中
        created = await _call(
            "bookkeeping_create",
            {
                "amount": 18.8,
                "flow": "expense",
                "category": "餐饮",
                "occurredAt": datetime.now(UTC).isoformat(),
            },
        )
        assert created.get("id")
        custom = await _call("bookkeeping_summary", today_plan["args"])
        assert custom["period"] == "custom"
        assert custom["count"] >= 1
        assert custom["expense"] >= 18.8


@pytest.mark.asyncio
async def test_a2ui_initialize_and_session_capability(
    app: FastAPI, fake_repos: dict[str, Any], tmp_path: Path
) -> None:
    """initialize 声明 Server 能力，Session 保存 experimental.a2ui 并影响后续结果。"""
    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read", "moments.write"))
    now = datetime.now(UTC)
    goal = HabitGoal(
        id=UUID("33333333-3333-4333-8333-333333333333"),
        user_id=USER_ID,
        name="晨跑",
        revision=1,
        created_at=now,
        updated_at=now,
        unit="公里",
        frequency="weekly",
        times_per_week=5,
    )
    await fake_repos["habit"].create(goal)

    async with _mcp_client(app) as client:
        session_id, initialized = await _initialize_with_result(
            client, token, capabilities=A2UI_CAPABILITIES
        )
        assert initialized["protocolVersion"] == PROTOCOL_VERSION
        # 2025-06-18 的 wire schema 会筛掉 2026 才标准化的 extensions 字段；
        # Session 仍会保留 experimental.a2ui，并用于后续 Tool Result 协商。
        assert "extensions" not in initialized["capabilities"]

        data = await _post(
            client,
            session_id,
            token,
            {
                "jsonrpc": "2.0",
                "id": 80,
                "method": "tools/call",
                "params": {
                    "name": "habit_progress",
                    "arguments": {"days": 7, "timezone": "Asia/Shanghai"},
                },
            },
        )

    result = data["result"]
    assert result["structuredContent"]["goals"][0]["name"] == "晨跑"
    assert any(item["type"] == "text" and item["text"] for item in result["content"])
    resource = next(item for item in result["content"] if item["type"] == "resource")
    assert resource["resource"]["mimeType"] == A2UI_MIME_TYPE
    assert resource["annotations"]["audience"] == ["user"]
    validate_a2ui_messages(json.loads(resource["resource"]["text"]))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capabilities",
    [
        {},
        {
            "experimental": {
                "a2ui": {
                    "clientCapabilities": {
                        A2UI_VERSION: {
                            "supportedCatalogIds": ["https://example.invalid/catalog.json"]
                        }
                    }
                }
            }
        },
    ],
)
async def test_non_a2ui_clients_keep_text_and_structured_fallback(
    app: FastAPI,
    tmp_path: Path,
    capabilities: dict[str, Any],
) -> None:
    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read",))
    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token, capabilities=capabilities)
        data = await _post(
            client,
            session_id,
            token,
            {
                "jsonrpc": "2.0",
                "id": 81,
                "method": "tools/call",
                "params": {
                    "name": "habit_progress",
                    "arguments": {"days": 7, "timezone": "UTC"},
                },
            },
        )
    result = data["result"]
    assert result["structuredContent"]["goals"] == []
    assert [item["type"] for item in result["content"]] == ["text"]


@pytest.mark.asyncio
async def test_agent_plan_routes_registered_schema_valid_tools(
    app: FastAPI, fake_repos: dict[str, Any], tmp_path: Path
) -> None:
    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read", "moments.write"))
    now = datetime.now(UTC)
    goal = HabitGoal(
        id=UUID("44444444-4444-4444-8444-444444444444"),
        user_id=USER_ID,
        name="晨跑",
        revision=1,
        created_at=now,
        updated_at=now,
        unit="公里",
        frequency="weekly",
        times_per_week=5,
    )
    await fake_repos["habit"].create(goal)

    async with _mcp_client(app) as client:
        session_id = await _initialize(client, token)
        listed = await _post(
            client,
            session_id,
            token,
            {"jsonrpc": "2.0", "id": 82, "method": "tools/list", "params": {}},
        )
        schemas = {tool["name"]: tool["inputSchema"] for tool in listed["result"]["tools"]}

        async def plan(user_input: str, request_id: int) -> dict[str, Any]:
            response = await _post(
                client,
                session_id,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": "agent_plan", "arguments": {"input": user_input}},
                },
            )
            return response["result"]["structuredContent"]

        cases = [
            ("这个月花了多少", "bookkeeping_summary"),
            ("记一笔午餐 28 元", "bookkeeping_create"),
            ("搜索上次去杭州的记忆", "moments_search"),
            ("查看我的习惯进度", "habit_progress"),
            ("晨跑打卡 3 公里", "habit_checkin_create"),
        ]
        for index, (user_input, expected_tool) in enumerate(cases, start=83):
            planned = await plan(user_input, index)
            assert planned["toolName"] == expected_tool, planned
            assert planned["toolName"] in schemas
            from jsonschema import Draft202012Validator

            assert not list(
                Draft202012Validator(schemas[expected_tool]).iter_errors(planned["arguments"])
            )
            assert planned["toolName"] != "agent_plan"

        unsupported = await plan("给我讲个笑话", 90)
        assert unsupported["toolName"] == ""
        assert unsupported["reply"]
        insufficient = await plan("帮我记一笔午餐", 91)
        assert insufficient["toolName"] == ""
        assert "金额" in insufficient["reply"]


@pytest.mark.asyncio
async def test_agent_plan_scope_and_a2ui_action_whitelist(app: FastAPI, tmp_path: Path) -> None:
    import sys

    mod = sys.modules[__name__]  # type: ignore[assignment]
    settings = _make_settings(tmp_path)
    token = _issue_mcp_token(settings, scope=("moments.read",))
    moment_id = "55555555-5555-4555-8555-555555555555"
    original_scope = mod.FAKE_AUTH_SCOPE  # type: ignore[attr-defined]
    mod.FAKE_AUTH_SCOPE = "moments.read"  # type: ignore[attr-defined]
    try:
        async with _mcp_client(app) as client:
            session_id = await _initialize(client, token)

            async def call(name: str, arguments: dict[str, Any], request_id: int) -> dict:
                return await _post(
                    client,
                    session_id,
                    token,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )

            denied = await call("agent_plan", {"input": "记一笔午餐 28 元"}, 92)
            assert denied["result"]["structuredContent"]["toolName"] == ""
            assert "moments.write" in denied["result"]["structuredContent"]["reply"]

            opened = await call(
                "a2ui_action",
                {
                    "name": "open_detail",
                    "surfaceId": "moment-search-1",
                    "context": {"momentId": moment_id},
                },
                93,
            )
            assert opened["result"]["structuredContent"]["toolName"] == "moments_get"

            refreshed = await call(
                "a2ui_action",
                {
                    "name": "refresh",
                    "surfaceId": "habit-progress-1",
                    "context": {"view": "habits"},
                },
                94,
            )
            assert refreshed["result"]["structuredContent"]["toolName"] == "habit_progress"

            invalid = await call(
                "a2ui_action",
                {
                    "name": "confirm",
                    "surfaceId": "habit-progress-1",
                    "context": {},
                },
                95,
            )
            assert invalid["result"]["isError"] is True
            assert "confirm" in invalid["result"]["content"][0]["text"]
    finally:
        mod.FAKE_AUTH_SCOPE = original_scope  # type: ignore[attr-defined]
