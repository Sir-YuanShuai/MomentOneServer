"""MCP 授权管理 API 测试（Web 端：列表 / 调整 scope / 撤销）。"""

# pyright: reportPrivateUsage=false
# pyright: reportPrivateImportUsage=false

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.api import deps as deps_module
from app.application import create_application
from app.core.config import Settings
from app.infrastructure.database.models import McpAuthorization
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

USER_ID = UUID("11111111-1111-4111-8111-111111111111")


def _make_settings(tmp_path: Path) -> Settings:
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
    return Settings(
        env="test",
        database_url="postgresql+psycopg://test:test@127.0.0.1:5432/test",
        jwt_private_key_path=str(priv_path),
        jwt_public_key_path=str(pub_path),
        jwt_issuer="https://momentone.test",
        jwt_audience="momentone-glasses",
    )


class FakeAuthzSession:
    """内存态 mcp_authorizations 会话。"""

    def __init__(self) -> None:
        self._store: dict[UUID, McpAuthorization] = {}
        self._by_user_client: dict[tuple[UUID, str], UUID] = {}
        self.last_touch: list[tuple[UUID, str]] = []

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def flush(self) -> None:
        pass

    def add(self, orm: McpAuthorization) -> None:
        self._store[orm.id] = orm
        self._by_user_client[(orm.user_id, orm.client_id)] = orm.id


class FakeAuthzResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalars(self) -> FakeAuthzScalars:
        return FakeAuthzScalars(self._value)


class FakeAuthzScalars:
    def __init__(self, value: Any) -> None:
        self._value = value

    def all(self) -> Any:
        if isinstance(self._value, list):
            return self._value
        return [self._value] if self._value else []


class FakeAuthzRepository:
    def __init__(self, session: FakeAuthzSession) -> None:
        self._session = session

    async def list_by_user(self, user_id: UUID) -> list[McpAuthorization]:
        return [a for a in self._session._store.values() if a.user_id == user_id]

    async def update_scope(
        self, *, authorization_id: UUID, user_id: UUID, scope: str
    ) -> McpAuthorization | None:
        a = self._session._store.get(authorization_id)
        if a is None or a.user_id != user_id:
            return None
        a.scope = scope
        return a

    async def revoke(self, *, authorization_id: UUID, user_id: UUID) -> McpAuthorization | None:
        a = self._session._store.get(authorization_id)
        if a is None or a.user_id != user_id:
            return None
        a.status = "revoked"
        return a

    async def get_by_user_and_client(
        self, user_id: UUID, client_id: str
    ) -> McpAuthorization | None:
        aid = self._session._by_user_client.get((user_id, client_id))
        return self._session._store.get(aid) if aid else None

    async def touch_active(self, *, user_id: UUID, client_id: str) -> None:
        self._session.last_touch.append((user_id, client_id))

    async def upsert(self, **kwargs: Any) -> McpAuthorization:
        raise NotImplementedError


@pytest.fixture
def fake_session() -> FakeAuthzSession:
    s = FakeAuthzSession()
    now = datetime.now(UTC)
    a1 = McpAuthorization(
        id=uuid4(),
        user_id=USER_ID,
        client_id="momentone-test-client",
        client_name="Claude Desktop",
        scope="moments.read moments.write",
        status="active",
        created_at=now,
        updated_at=now,
    )
    a2 = McpAuthorization(
        id=uuid4(),
        user_id=USER_ID,
        client_id="momentone-test-client2",
        client_name="ChatGPT",
        scope="moments.read",
        status="active",
        created_at=now,
        updated_at=now,
    )
    s.add(a1)
    s.add(a2)
    return s


@pytest.fixture
def app(tmp_path: Path, fake_session: FakeAuthzSession) -> Iterator[FastAPI]:
    settings = _make_settings(tmp_path)
    application = create_application(settings)

    from app.api.routes import mcp_authorizations as routes

    async def _fake_auth() -> deps_module.AuthContext:
        return deps_module.AuthContext(user_id=USER_ID, method="casdoor")

    async def _fake_user_id() -> UUID:
        return USER_ID

    application.dependency_overrides[deps_module.get_auth_context] = _fake_auth
    application.dependency_overrides[deps_module.get_authenticated_user_id] = _fake_user_id
    application.dependency_overrides[routes.get_db_session] = lambda: fake_session

    original = routes.McpAuthorizationRepository
    routes.McpAuthorizationRepository = lambda session: FakeAuthzRepository(session)  # type: ignore[assignment]
    yield application
    routes.McpAuthorizationRepository = original


@pytest.mark.asyncio
async def test_list_authorizations(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/v1/mcp/authorizations")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert rows[0]["clientName"] in ("Claude Desktop", "ChatGPT")
    assert rows[0]["scope"] == ["moments.read", "moments.write"]


@pytest.mark.asyncio
async def test_update_scope(app: FastAPI, fake_session: FakeAuthzSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        target = next(
            a for a in fake_session._store.values() if a.client_id == "momentone-test-client2"
        )
        resp = await client.patch(
            f"/v1/mcp/authorizations/{target.id}",
            json={"scope": ["moments.read", "moments.write"]},
        )
    assert resp.status_code == 200
    assert resp.json()["scope"] == ["moments.read", "moments.write"]


@pytest.mark.asyncio
async def test_update_scope_rejects_unknown(app: FastAPI, fake_session: FakeAuthzSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        target = next(iter(fake_session._store.values()))
        resp = await client.patch(
            f"/v1/mcp/authorizations/{target.id}",
            json={"scope": ["bad.scope"]},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_revoke_authorization(app: FastAPI, fake_session: FakeAuthzSession) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        target = next(
            a for a in fake_session._store.values() if a.client_id == "momentone-test-client"
        )
        resp = await client.delete(f"/v1/mcp/authorizations/{target.id}")
        assert resp.status_code == 204
    assert target.status == "revoked"


@pytest.mark.asyncio
async def test_revoke_not_found(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.delete(f"/v1/mcp/authorizations/{uuid4()}")
    assert resp.status_code == 404
