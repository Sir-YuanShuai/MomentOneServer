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
