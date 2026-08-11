from uuid import UUID

import pytest
from app.application import create_application
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.infrastructure.identity.casdoor import (
    AuthenticatedPrincipal,
    CasdoorTokenVerifier,
    _claim_bool,
    _claim_names,
    _claim_optional_bool,
)
from app.modules.admin.auth import AdminContext, get_admin_context, permissions_for_principal
from httpx import ASGITransport, AsyncClient


def test_casdoor_admin_claim_normalization() -> None:
    assert _claim_bool(True)
    assert _claim_bool("true")
    assert not _claim_bool("false")
    assert _claim_optional_bool("false") is False
    assert _claim_optional_bool("true") is True
    assert _claim_optional_bool(None) is None
    assert _claim_names(["momentone-admin", {"name": "ops"}, {"id": "permission-id"}]) == (
        "momentone-admin",
        "ops",
        "permission-id",
    )


def test_casdoor_userinfo_verification_state_overrides_stale_token_claim() -> None:
    principal = AuthenticatedPrincipal(
        issuer="https://example.com",
        subject="user",
        email_verified=True,
        phone_verified=True,
    ).merge_userinfo({"emailVerified": False, "phoneVerified": "false"})

    assert principal.email_verified is False
    assert principal.phone_verified is False


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


def test_casdoor_organization_guard_rejects_other_owner() -> None:
    verifier = CasdoorTokenVerifier(Settings(env="test", casdoor_organization="yuanshuai.fun"))

    with pytest.raises(ApplicationError) as exc_info:
        verifier.ensure_organization(
            AuthenticatedPrincipal(
                issuer="https://example.com",
                subject="user",
                owner="built-in",
            )
        )

    assert exc_info.value.code == "IDENTITY_ORGANIZATION_MISMATCH"
    assert exc_info.value.status_code == 401


def test_casdoor_organization_guard_accepts_account_owner() -> None:
    verifier = CasdoorTokenVerifier(Settings(env="test", casdoor_organization="yuanshuai.fun"))
    principal = AuthenticatedPrincipal(
        issuer="https://example.com",
        subject="user",
    ).merge_userinfo({"owner": "yuanshuai.fun"})

    verifier.ensure_organization(principal)


def test_casdoor_account_scope_rejects_other_application() -> None:
    verifier = CasdoorTokenVerifier(
        Settings(
            env="test",
            casdoor_organization="yuanshuai.fun",
            casdoor_application="MomentOne",
        )
    )

    with pytest.raises(ApplicationError) as exc_info:
        verifier.ensure_account_scope({"owner": "yuanshuai.fun", "signupApplication": "AnotherApp"})

    assert exc_info.value.code == "IDENTITY_ORGANIZATION_MISMATCH"


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
