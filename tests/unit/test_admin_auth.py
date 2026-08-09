from uuid import UUID

import pytest
from app.application import create_application
from app.core.config import Settings
from app.infrastructure.identity.casdoor import AuthenticatedPrincipal, _claim_bool, _claim_names
from app.modules.admin.auth import AdminContext, get_admin_context, permissions_for_principal
from httpx import ASGITransport, AsyncClient


def test_casdoor_admin_claim_normalization() -> None:
    assert _claim_bool(True)
    assert _claim_bool("true")
    assert not _claim_bool("false")
    assert _claim_names(["momentone-admin", {"name": "ops"}, {"id": "permission-id"}]) == (
        "momentone-admin",
        "ops",
        "permission-id",
    )


def test_admin_role_and_operator_permissions() -> None:
    settings = Settings(env="test")
    admin = AuthenticatedPrincipal(
        issuer="https://example.com", subject="admin", roles=("momentone-admin",)
    )
    permissions, is_super, source = permissions_for_principal(admin, settings)
    assert permissions == {
        "admin.read",
        "admin.users",
        "admin.security",
        "admin.operations",
    }
    assert is_super is True
    assert source == "casdoor.role"

    operator = AuthenticatedPrincipal(
        issuer="https://example.com", subject="operator", roles=("momentone-operator",)
    )
    permissions, is_super, source = permissions_for_principal(operator, settings)
    assert permissions == {"admin.read", "admin.security", "admin.operations"}
    assert is_super is False
    assert source == "casdoor.permission"


@pytest.mark.asyncio
async def test_admin_session_contract() -> None:
    app = create_application(Settings(env="test", allowed_origins=[]))
    app.dependency_overrides[get_admin_context] = lambda: AdminContext(
        user_id=UUID("11111111-1111-4111-8111-111111111111"),
        subject="built-in/admin",
        display_name="Admin",
        email="admin@example.com",
        permissions=frozenset({"admin.read", "admin.users"}),
        is_super_admin=True,
        source="casdoor.isAdmin",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/admin/session")
    assert response.status_code == 200
    assert response.json() == {
        "isAdmin": True,
        "isSuperAdmin": True,
        "permissions": ["admin.read", "admin.users"],
        "source": "casdoor.isAdmin",
        "identity": {
            "subject": "built-in/admin",
            "displayName": "Admin",
            "email": "admin@example.com",
        },
    }
