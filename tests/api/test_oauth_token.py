"""OAuth Token 端点 API 测试。

通过 dependency_overrides 注入 Fake Service，不依赖数据库和 Casdoor。
测试 HTTP 层：请求解析、响应格式、错误码映射。
"""

from pathlib import Path
from uuid import UUID

import pytest
from app.api.routes import oauth as oauth_routes
from app.application import create_application
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.devices.domain import (
    TokenResponse as DomainTokenResponse,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
BINDING_ID = UUID("22222222-2222-4222-8222-222222222222")
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


class FakeService:
    """可编程的 Fake Service，按预设结果返回。"""

    def __init__(self, *, jwt_issuer: JwtIssuer, settings: Settings) -> None:
        self._jwt_issuer = jwt_issuer
        self._settings = settings
        self.complete_binding_result: DomainTokenResponse | None = None
        self.refresh_result: DomainTokenResponse | None = None
        self.complete_binding_error: ApplicationError | None = None
        self.refresh_error: ApplicationError | None = None
        self.complete_calls: list[dict] = []
        self.refresh_calls: list[str] = []

    async def complete_binding(
        self,
        *,
        binding_code: str,
        device_id: str,
        device_name: str | None,
        device_type: str | None,
    ) -> DomainTokenResponse:
        self.complete_calls.append(
            {
                "binding_code": binding_code,
                "device_id": device_id,
                "device_name": device_name,
                "device_type": device_type,
            }
        )
        if self.complete_binding_error:
            raise self.complete_binding_error
        if self.complete_binding_result is None:
            # 默认成功结果
            access_token, _ = self._jwt_issuer.issue_access_token(
                binding_id=BINDING_ID,
                user_id=USER_ID,
                device_id=device_id,
                scope=("moments.read", "moments.write"),
            )
            refresh_token = self._jwt_issuer.issue_refresh_token(
                binding_id=BINDING_ID,
                user_id=USER_ID,
                device_id=device_id,
                scope=("moments.read", "moments.write"),
            )
            return DomainTokenResponse(
                binding_id=BINDING_ID,
                access_token=access_token,
                refresh_token=refresh_token,
                token_type="Bearer",
                expires_in=3600,
                scope="moments.read moments.write",
            )
        return self.complete_binding_result

    async def refresh_access_token(self, refresh_token: str) -> DomainTokenResponse:
        self.refresh_calls.append(refresh_token)
        if self.refresh_error:
            raise self.refresh_error
        if self.refresh_result is None:
            access_token, _ = self._jwt_issuer.issue_access_token(
                binding_id=BINDING_ID,
                user_id=USER_ID,
                device_id=DEVICE_ID,
                scope=("moments.read",),
            )
            new_refresh = self._jwt_issuer.issue_refresh_token(
                binding_id=BINDING_ID,
                user_id=USER_ID,
                device_id=DEVICE_ID,
                scope=("moments.read",),
            )
            return DomainTokenResponse(
                binding_id=BINDING_ID,
                access_token=access_token,
                refresh_token=new_refresh,
                token_type="Bearer",
                expires_in=3600,
                scope="moments.read",
            )
        return self.refresh_result


@pytest.fixture
def fake_service(tmp_path: Path) -> FakeService:
    settings = _make_settings(tmp_path)
    issuer = JwtIssuer(settings)
    return FakeService(jwt_issuer=issuer, settings=settings)


@pytest.fixture
def app(tmp_path: Path, fake_service: FakeService) -> FastAPI:
    settings = _make_settings(tmp_path)
    app = create_application(settings)
    app.dependency_overrides[oauth_routes._make_service] = lambda: fake_service  # pyright: ignore[reportPrivateUsage]
    # MCP OAuth grant 测试在 test_mcp_oauth.py 覆盖；此处提供空实现避免解析 DB 依赖
    app.dependency_overrides[
        oauth_routes._make_mcp_oauth_service  # pyright: ignore[reportPrivateUsage]
    ] = lambda: _FakeMcpOAuthService()
    return app


class _FakeMcpOAuthService:
    """仅占位：QR binding / glasses refresh 测试不触发 MCP grant。"""

    async def exchange_auth_code(self, **kwargs: object) -> object:
        raise AssertionError("authorization_code grant 应由 test_mcp_oauth 覆盖")

    async def refresh_mcp_token(self, refresh_token: str) -> object:
        raise AssertionError("MCP refresh 应由 test_mcp_oauth 覆盖")


@pytest.mark.asyncio
async def test_token_qr_binding_success(app: FastAPI, fake_service: FakeService) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "urn:momentone:oauth:grant-type:qr-binding",
                "binding_code": "BIND-abcdef123456",
                "device_id": DEVICE_ID,
                "device_name": "Rokid",
                "device_type": "glasses",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["scope"] == "moments.read moments.write"
    assert body["binding_id"] == str(BINDING_ID)
    assert body["access_token"]
    assert body["refresh_token"]
    assert len(fake_service.complete_calls) == 1
    assert fake_service.complete_calls[0]["binding_code"] == "BIND-abcdef123456"
    assert fake_service.complete_calls[0]["device_id"] == DEVICE_ID


@pytest.mark.asyncio
async def test_token_qr_binding_missing_params(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "urn:momentone:oauth:grant-type:qr-binding",
                # 缺少 binding_code 和 device_id
            },
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_token_refresh_success(app: FastAPI, fake_service: FakeService) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": "some-old-refresh-token",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["scope"] == "moments.read"
    assert len(fake_service.refresh_calls) == 1
    assert fake_service.refresh_calls[0] == "some-old-refresh-token"


@pytest.mark.asyncio
async def test_token_refresh_missing_token(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_token_unsupported_grant_type(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/oauth/token",
            data={"grant_type": "password", "username": "x", "password": "y"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_token_qr_binding_service_error(app: FastAPI, fake_service: FakeService) -> None:
    fake_service.complete_binding_error = ApplicationError(
        code="BINDING_CODE_EXPIRED",
        message="binding_code 已过期。",
        status_code=400,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "urn:momentone:oauth:grant-type:qr-binding",
                "binding_code": "BIND-expired",
                "device_id": DEVICE_ID,
            },
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_token_refresh_service_error(app: FastAPI, fake_service: FakeService) -> None:
    fake_service.refresh_error = ApplicationError(
        code="REFRESH_TOKEN_INVALID",
        message="refresh_token 验签失败。",
        status_code=401,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": "invalid-token",
            },
        )
    assert resp.status_code == 401
