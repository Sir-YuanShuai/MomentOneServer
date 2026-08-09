import pytest
from app.application import create_application
from app.core.config import Settings
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def app_settings() -> Settings:
    return Settings(env="test", debug=True, allowed_origins=[])


@pytest.mark.asyncio
async def test_healthz(app_settings: Settings) -> None:
    app = create_application(app_settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_readyz(app_settings: Settings) -> None:
    app = create_application(app_settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"application": "ok"}}


@pytest.mark.asyncio
async def test_version(app_settings: Settings) -> None:
    app = create_application(app_settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/version")

    assert response.status_code == 200
    assert response.json() == {"name": "moment-one-server", "version": "0.1.0"}


def test_admin_entitlement_routes_registered(app_settings: Settings) -> None:
    paths = create_application(app_settings).openapi()["paths"]
    expected = {
        "/v1/admin/plans",
        "/v1/admin/storage/summary",
        "/v1/admin/storage/accounts",
        "/v1/admin/users/{user_id}/entitlements",
        "/v1/admin/users/{user_id}/plan",
        "/v1/admin/users/{user_id}/storage-grants",
        "/v1/admin/storage-grants/{grant_id}/revoke",
        "/v1/admin/users/{user_id}/storage/reconcile",
    }
    assert expected.issubset(paths)
