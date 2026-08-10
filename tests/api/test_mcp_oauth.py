"""MCP OAuth 全链路测试（DCR → authorize → Casdoor 回调 → token 交换 → refresh）。

通过 monkeypatch 替换 Casdoor 代理与 Repository（不依赖真实 Casdoor / DB）：
- POST /oauth/register（RFC 7591）
- GET /oauth/authorize → Casdoor 跳转 URL（state + dual-PKCE）
- GET /oauth/callback → 我方授权码 + 302 回客户端
- POST /oauth/token（authorization_code + PKCE → RS256 token）
- POST /oauth/token（refresh_token）
- 错误路径：PKCE 失败 / 授权码过期 / 未知客户端
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pytest
from app.api.routes import mcp_oauth as mcp_oauth_routes
from app.application import create_application
from app.core.config import Settings
from app.infrastructure.identity.casdoor_management import CasdoorManagementClient
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.mcp_oauth import service as oauth_service
from app.modules.mcp_oauth.service import (
    derive_code_challenge,
    generate_code_verifier,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
CASDOOR_SUB = "casdoor-user-1"


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
    apps_html.write_text("<html><body>test</body></html>", encoding="utf-8")
    return Settings(
        env="test",
        database_url="postgresql+psycopg://test:test@127.0.0.1:5432/test",
        jwt_private_key_path=str(priv_path),
        jwt_public_key_path=str(pub_path),
        jwt_issuer="https://momentone.test",
        jwt_audience="momentone-glasses",
        access_token_ttl_seconds=3600,
        refresh_token_ttl_seconds=90 * 24 * 3600,
        mcp_base_url="http://testserver",
        casdoor_issuer="https://account.example.fun",  # type: ignore[arg-type]
        casdoor_mcp_client_id="mcp-proxy-client",
        casdoor_mcp_client_secret="proxy-secret",
        casdoor_mcp_redirect_uri=None,  # 显式覆盖 .env，避免回退到真实环境变量
        mcp_apps_html_path=str(apps_html),
    )


def test_casdoor_account_link_authorize_url_forces_login_without_provider_param(
    tmp_path: Path,
) -> None:
    settings = _make_settings(tmp_path)
    client = CasdoorManagementClient(settings)

    url = client.authorize_url(
        state="link-state",
        code_verifier="link-verifier",
        redirect_uri="http://localhost:8000/oauth/callback",
    )
    params = parse_qs(urlparse(url).query)

    assert urlparse(url).netloc == "account.example.fun"
    assert params["prompt"] == ["login"]
    assert params["max_age"] == ["0"]
    assert params["redirect_uri"] == ["http://localhost:8000/oauth/callback"]
    assert params["code_challenge_method"] == ["S256"]
    assert "provider" not in params


@pytest.mark.asyncio
async def test_casdoor_provider_link_url_uses_link_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings(tmp_path)

    async def fake_get_application(self: CasdoorManagementClient) -> dict[str, object]:
        del self
        return {
            "name": "MomentOne",
            "providers": [
                {
                    "provider": {
                        "type": "GitHub",
                        "name": "oAuth-github",
                        "clientId": "github-client",
                        "scopes": "",
                    }
                }
            ],
        }

    monkeypatch.setattr(CasdoorManagementClient, "get_application", fake_get_application)
    client = CasdoorManagementClient(settings)

    url = await client.provider_link_url(
        provider="github",
        return_uri="http://localhost:8000/oauth/link-return?state=link-state",
    )
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    inner = parse_qs(base64.b64decode(params["state"][0]).decode().lstrip("&"))

    assert parsed.netloc == "github.com"
    assert params["client_id"] == ["github-client"]
    assert params["redirect_uri"] == ["https://account.example.fun/callback"]
    assert inner["application"] == ["MomentOne"]
    assert inner["provider"] == ["oAuth-github"]
    assert inner["method"] == ["link"]
    assert inner["from"] == ["http://localhost:8000/oauth/link-return?state=link-state"]


class _FakeExecResult:
    def scalar_one_or_none(self) -> None:
        return None

    def scalars(self) -> _FakeScalars:
        return _FakeScalars()


class _FakeScalars:
    def all(self) -> list:
        return []


class FakeSession:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def execute(self, stmt: object) -> _FakeExecResult:
        return _FakeExecResult()

    async def flush(self) -> None:
        pass

    def add(self, orm: object) -> None:
        pass


@dataclass
class FakeClient:
    id: UUID
    client_id: str
    client_name: str | None
    redirect_uris: list[str]
    scope: str
    grant_types: list[str]
    token_endpoint_auth_method: str
    status: str
    created_at: datetime


class FakeClientRepo:
    def __init__(self) -> None:
        self._store: dict[str, FakeClient] = {}

    async def create(
        self,
        *,
        client_id: str,
        client_name: str | None,
        redirect_uris: list[str],
        scope: str,
        grant_types: list[str],
        token_endpoint_auth_method: str = "none",
    ) -> FakeClient:
        c = FakeClient(
            id=uuid4(),
            client_id=client_id,
            client_name=client_name,
            redirect_uris=redirect_uris,
            scope=scope,
            grant_types=grant_types,
            token_endpoint_auth_method=token_endpoint_auth_method,
            status="active",
            created_at=datetime.now(UTC),
        )
        self._store[client_id] = c
        return c

    async def get_by_client_id(self, client_id: str) -> FakeClient | None:
        return self._store.get(client_id)


@dataclass
class FakeCode:
    id: UUID
    code: str
    kind: str
    client_id: str
    redirect_uri: str | None
    scope: str | None
    state: str | None
    code_challenge: str | None
    casdoor_code_verifier: str | None
    resource: str | None
    user_id: UUID | None
    status: str
    expires_at: datetime


class FakeCodeRepo:
    def __init__(self) -> None:
        self._store: dict[str, FakeCode] = {}

    async def create(
        self,
        *,
        code: str,
        kind: str,
        client_id: str,
        redirect_uri: str | None,
        scope: str | None,
        state: str | None,
        code_challenge: str | None,
        casdoor_code_verifier: str | None,
        resource: str | None,
        user_id: UUID | None,
        expires_at: datetime,
    ) -> FakeCode:
        c = FakeCode(
            id=uuid4(),
            code=code,
            kind=kind,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            state=state,
            code_challenge=code_challenge,
            casdoor_code_verifier=casdoor_code_verifier,
            resource=resource,
            user_id=user_id,
            status="pending",
            expires_at=expires_at,
        )
        self._store[code] = c
        return c

    async def get_by_code(self, code: str) -> FakeCode | None:
        return self._store.get(code)

    async def mark_consumed(self, *, code_id: UUID) -> None:
        for c in self._store.values():
            if c.id == code_id:
                c.status = "consumed"


class FakeCasdoorProxy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.exchange_calls: list[dict] = []

    def authorize_url(self, *, state: str, code_verifier: str, redirect_uri: str) -> str:
        return (
            f"{str(self.settings.casdoor_issuer).rstrip('/')}/login/oauth/authorize"
            f"?client_id={self.settings.casdoor_mcp_client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&state={state}&code_challenge={derive_code_challenge(code_verifier)}"
            "&code_challenge_method=S256&scope=openid%20profile%20email"
        )

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> dict:
        self.exchange_calls.append(
            {"code": code, "code_verifier": code_verifier, "redirect_uri": redirect_uri}
        )
        return {"access_token": "fake-casdoor-access", "id_token": "fake-id"}


async def _fake_resolve_user_id(session: object, verifier: object, token: str) -> UUID:
    return USER_ID


@pytest.fixture
def fakes() -> dict[str, Any]:
    return {
        "client_repo": FakeClientRepo(),
        "code_repo": FakeCodeRepo(),
        "casdoor": FakeCasdoorProxy,
        "resolve_user_id": _fake_resolve_user_id,
    }


@pytest.fixture
def app(tmp_path: Path, fakes: dict[str, Any]) -> Iterator[FastAPI]:
    settings = _make_settings(tmp_path)
    application = create_application(settings)

    # 替换 service 内的 Repository / Casdoor 代理 / 用户解析
    original = {
        "client_repo": oauth_service.McpClientRepository,
        "code_repo": oauth_service.McpAuthCodeRepository,
        "casdoor": oauth_service.CasdoorProxyClient,
        "resolve": oauth_service.resolve_user_id,
    }
    oauth_service.McpClientRepository = lambda session: fakes["client_repo"]  # type: ignore[assignment]
    oauth_service.McpAuthCodeRepository = lambda session: fakes["code_repo"]  # type: ignore[assignment]
    oauth_service.CasdoorProxyClient = fakes["casdoor"]  # type: ignore[assignment]
    oauth_service.resolve_user_id = fakes["resolve_user_id"]  # type: ignore[assignment]

    async def _fake_session() -> FakeSession:
        return FakeSession()

    from app.core.config import get_settings

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[mcp_oauth_routes.get_db_session] = _fake_session
    application.dependency_overrides[mcp_oauth_routes.get_quota_repository] = lambda: None

    class FakeAccountCenter:
        async def has_link_state(self, state: str) -> bool:
            del state
            return False

    application.dependency_overrides[mcp_oauth_routes._make_account_center_service] = (  # pyright: ignore[reportPrivateUsage]
        FakeAccountCenter
    )

    yield application

    for name, cls in original.items():
        setattr(oauth_service, name, cls)


async def _register_client(client: AsyncClient) -> dict:
    resp = await client.post(
        "/oauth/register",
        json={
            "client_name": "Claude Desktop",
            "redirect_uris": ["http://127.0.0.1:4321/callback"],
            "scope": "moments.read moments.write",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_oauth_register(app: FastAPI) -> None:
    """RFC 7591 动态客户端注册。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        reg = await _register_client(client)
    assert reg["client_id"].startswith("momentone-")
    assert reg["scope"] == "moments.read moments.write"
    assert reg["token_endpoint_auth_method"] == "none"
    assert reg["redirect_uris"] == ["http://127.0.0.1:4321/callback"]


@pytest.mark.asyncio
async def test_oauth_authorize_redirects_to_casdoor(app: FastAPI) -> None:
    """authorize：校验客户端 + PKCE → 302 跳转 Casdoor（state + dual-PKCE）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        reg = await _register_client(client)
        verifier = generate_code_verifier()
        challenge = derive_code_challenge(verifier)

        resp = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": reg["client_id"],
                "redirect_uri": "http://127.0.0.1:4321/callback",
                "scope": "moments.read",
                "state": "client-state-123",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
    assert resp.status_code == 302, resp.text
    url = resp.headers["location"]
    parsed = urlparse(url)
    assert parsed.netloc == "account.example.fun"
    assert parsed.path == "/login/oauth/authorize"
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["mcp-proxy-client"]
    assert qs["redirect_uri"] == ["http://testserver/oauth/callback"]
    assert "code_challenge" in qs  # dual-PKCE：我方对 Casdoor 也发 challenge
    assert qs["code_challenge_method"] == ["S256"]


@pytest.mark.asyncio
async def test_oauth_full_flow(app: FastAPI, tmp_path: Path) -> None:
    """全链路：DCR → authorize → 回调 → 授权码换 RS256 token → 刷新。"""
    settings = _make_settings(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        reg = await _register_client(client)
        verifier = generate_code_verifier()
        challenge = derive_code_challenge(verifier)

        authz = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": reg["client_id"],
                "redirect_uri": "http://127.0.0.1:4321/callback",
                "scope": "moments.read moments.write",
                "state": "client-state-456",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert authz.status_code == 302
        casdoor_url = urlparse(authz.headers["location"])
        casdoor_state = parse_qs(casdoor_url.query)["state"][0]

        # Casdoor 回调（带上 Casdoor 的授权码 + state）
        cb = await client.get(
            "/oauth/callback",
            params={"code": "casdoor-auth-code", "state": casdoor_state},
        )
        assert cb.status_code == 302
        client_redirect = cb.headers["location"]
        assert client_redirect.startswith("http://127.0.0.1:4321/callback?code=")
        cb_qs = parse_qs(urlparse(client_redirect).query)
        auth_code = cb_qs["code"][0]
        assert cb_qs["state"] == ["client-state-456"]

        # 授权码 + PKCE verifier 换 token
        tok = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": reg["client_id"],
                "code": auth_code,
                "code_verifier": verifier,
                "redirect_uri": "http://127.0.0.1:4321/callback",
            },
        )
        assert tok.status_code == 200, tok.text
        body = tok.json()
        assert body["token_type"] == "Bearer"
        assert body["scope"] == "moments.read moments.write"
        assert body["access_token"]
        assert body["refresh_token"]

        # access_token 是 Server 自签 RS256，且能验签出本地用户
        payload = JwtIssuer(settings).verify_access_token(body["access_token"])
        assert payload["sub"] == str(USER_ID)
        assert payload["grant"] == "authorization_code"
        assert set(payload["scope"].split()) == {"moments.read", "moments.write"}

        # 授权码一次性：重复交换 → INVALID_GRANT
        tok2 = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": reg["client_id"],
                "code": auth_code,
                "code_verifier": verifier,
                "redirect_uri": "http://127.0.0.1:4321/callback",
            },
        )
        assert tok2.status_code == 400
        assert tok2.json()["error"]["code"] == "INVALID_GRANT"

        # refresh_token 换新 access_token（不滚动，返回原 refresh）
        ref = await client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": body["refresh_token"]},
        )
        assert ref.status_code == 200, ref.text
        ref_body = ref.json()
        assert ref_body["access_token"]
        assert ref_body["refresh_token"] == body["refresh_token"]


@pytest.mark.asyncio
async def test_oauth_pkce_mismatch(app: FastAPI) -> None:
    """PKCE verifier 不匹配 → INVALID_GRANT。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        reg = await _register_client(client)
        verifier = generate_code_verifier()
        challenge = derive_code_challenge(verifier)

        authz = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": reg["client_id"],
                "redirect_uri": "http://127.0.0.1:4321/callback",
                "scope": "moments.read",
                "state": "s",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        casdoor_state = parse_qs(urlparse(authz.headers["location"]).query)["state"][0]
        cb = await client.get("/oauth/callback", params={"code": "c1", "state": casdoor_state})
        assert cb.status_code == 302
        auth_code = parse_qs(urlparse(cb.headers["location"]).query)["code"][0]

        tok = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": reg["client_id"],
                "code": auth_code,
                "code_verifier": "wrong-verifier",
                "redirect_uri": "http://127.0.0.1:4321/callback",
            },
        )
    assert tok.status_code == 400
    assert tok.json()["error"]["code"] == "INVALID_GRANT"


@pytest.mark.asyncio
async def test_oauth_authorize_rejects_unknown_client(app: FastAPI) -> None:
    """未注册 client_id → INVALID_CLIENT。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "unknown-client",
                "redirect_uri": "http://127.0.0.1:4321/callback",
                "code_challenge": derive_code_challenge(generate_code_verifier()),
                "code_challenge_method": "S256",
            },
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_CLIENT"


@pytest.mark.asyncio
async def test_oauth_register_rejects_bad_metadata(app: FastAPI) -> None:
    """DCR：redirect_uris 缺失/非法 → INVALID_CLIENT_METADATA。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/oauth/register",
            json={"client_name": "bad", "redirect_uris": ["not-a-url"]},
        )
        resp2 = await client.post("/oauth/register", json={"client_name": "empty"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_CLIENT_METADATA"
    assert resp2.status_code == 400
