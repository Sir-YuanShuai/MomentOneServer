from collections.abc import AsyncGenerator
from types import SimpleNamespace
from uuid import UUID

import pytest
from app.api.deps import AuthContext
from app.api.routes import account as account_routes
from app.api.routes import admin as admin_routes
from app.application import create_application
from app.modules.admin.auth import AdminContext
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

USER_ID = UUID("22222222-2222-4222-8222-222222222222")


def test_login_providers_are_derived_from_casdoor_properties() -> None:
    providers = account_routes._login_providers(  # pyright: ignore[reportPrivateUsage]
        {
            "github": "",
            "properties": {
                "oauth_GitHub_id": "github-id",
                "oauth_GitHub_displayName": "octocat",
                "oauth_Gitee_id": "gitee-id",
                "oauth_Google_accessToken": "present",
                "oauth_Google_email": "user@gmail.com",
                "oauth_QQ_displayName": "qq-user",
                "oauth_QQ_id": "qq-id",
            },
        }
    )

    assert providers == [
        {"provider": "github", "handle": "octocat"},
        {"provider": "gitee", "handle": "gitee-id"},
        {"provider": "google", "handle": "user@gmail.com"},
        {"provider": "qq", "handle": "qq-user"},
    ]


def test_login_provider_direct_fields_do_not_count_as_bound() -> None:
    providers = account_routes._login_providers(  # pyright: ignore[reportPrivateUsage]
        {
            "github": "stale-github-id",
            "gitee": "stale-gitee-id",
            "google": "stale-google-id",
            "qq": "stale-qq-id",
            "properties": {},
        }
    )

    assert providers == []


class FakeAccountRepository:
    async def account(
        self, user_id: UUID, *, avatar_url: str | None = None
    ) -> dict[str, object] | None:
        assert user_id == USER_ID
        return {
            "user": {"id": str(user_id), "status": "active"},
            "profile": {
                "displayName": "Moment User",
                "email": "user@example.com",
                "avatarUrl": "https://example.com/avatar.png",
            },
            "plan": {"key": "plus", "version": 3},
            "storage": {"usedBytes": 100, "effectiveQuotaBytes": 1000},
            "entitlements": [{"key": "moment.core"}],
            "quotaAccounts": [
                {
                    "quotaKey": "mcp.tool_calls.month",
                    "limit": 10000,
                    "used": 42,
                    "remaining": 9958,
                }
            ],
        }

    async def admin_user_detail(self, user_id: UUID) -> dict[str, object] | None:
        return {
            "user": {"id": str(user_id), "status": "active"},
            "profile": {"displayName": "Moment User"},
            "plan": {"key": "plus"},
            "storage": {"usedBytes": 100},
            "quotaAccounts": [],
            "quotaUsage30d": {"totalAmount": 12, "activeDays": 4},
            "access": {
                "deviceBindings": {"total": 2, "active": 1},
                "mcpAuthorizations": {"total": 3, "active": 2},
            },
        }


class FakeAccountCenterService:
    async def update_profile(self, user_id: UUID, **kwargs: object) -> dict[str, object]:
        assert user_id == USER_ID
        return {
            "profile": {
                "displayName": "Test User",
                "locale": kwargs.get("locale", "zh-CN"),
                "timezone": kwargs.get("timezone"),
                "syncStatus": "synced",
            }
        }

    async def identities(self, user_id: UUID, **_kwargs: object) -> dict[str, object]:
        assert user_id == USER_ID
        return {
            "items": [
                {
                    "id": "44444444-4444-4444-8444-444444444444",
                    "type": "oidc",
                    "provider": "casdoor",
                    "identifier": "user@example.com",
                    "isPrimary": True,
                }
            ],
            "security": {"managementAvailable": True},
        }

    async def start_contact_challenge(self, user_id: UUID, **kwargs: object) -> dict[str, object]:
        assert user_id == USER_ID
        return {
            "challengeId": "55555555-5555-4555-8555-555555555555",
            "kind": kwargs["kind"],
            "maskedDestination": "us***@example.com",
            "expiresAt": "2026-08-09T12:00:00+00:00",
        }

    async def start_link_session(self, user_id: UUID, **_kwargs: object) -> dict[str, object]:
        assert user_id == USER_ID
        return {
            "linkSessionId": "66666666-6666-4666-8666-666666666666",
            "authorizeUrl": "https://account.example.com/authorize",
            "expiresAt": "2026-08-09T12:00:00+00:00",
        }

    async def unlink_login_provider(self, user_id: UUID, **kwargs: object) -> None:
        assert user_id == USER_ID
        assert kwargs["provider"] == "github"
        assert kwargs["access_token"] is None

    async def unlink_preview(self, user_id: UUID, identity_id: UUID) -> dict[str, object]:
        assert user_id == USER_ID
        return {
            "confirmationId": "77777777-7777-4777-8777-777777777777",
            "confirmationPhrase": "解除绑定",
            "identity": {"id": str(identity_id)},
        }

    async def merge_preview(self, user_id: UUID, link_session_id: UUID) -> dict[str, object]:
        assert user_id == USER_ID
        return {"linkSessionId": str(link_session_id), "status": "merge_required"}


class FakeAnalyticsRepository:
    async def usage_overview(self, *, days: int) -> dict[str, object]:
        return {
            "days": days,
            "todayActive": 8,
            "monthlyActive": 31,
            "apiRequests": 500,
            "apiErrors": 4,
            "mcpToolCalls": 90,
            "mcpWriteCalls": 12,
            "agentPlanCalls": 7,
            "aiTokens": 1000,
            "series": [],
            "endpoints": [],
            "topTools": [],
        }


class FakePlanRepository:
    async def list_plans(self) -> list[dict[str, object]]:
        return [{"key": "free", "version": 2}]

    async def create(self, **kwargs: object) -> dict[str, object] | None:
        return {**kwargs, "version": 1, "createdAt": "2026-08-09T00:00:00+00:00"}

    async def update(self, **kwargs: object) -> dict[str, object] | None:
        expected_version = kwargs["expected_version"]
        assert isinstance(expected_version, int)
        return {
            "key": kwargs["key"],
            "version": expected_version + 1,
            "name": kwargs["name"],
            "status": kwargs["status"],
            "entitlements": kwargs["entitlements"],
            "quotas": kwargs["quotas"],
        }

    async def version(self, key: str) -> int | None:
        return 2 if key == "plus" else None


class FakeAuditRepository:
    def __init__(self) -> None:
        self.received_query: str | None = None

    async def list_audit_events(self, **kwargs: object):
        self.received_query = kwargs.get("query")  # type: ignore[assignment]
        return [], False, None


class FakeIdempotencyRepository:
    async def complete(self, **_kwargs: object) -> None:
        return None


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    application = create_application()
    admin_context = AdminContext(
        user_id=USER_ID,
        subject="admin-sub",
        display_name="Admin",
        email="admin@example.com",
        permissions=frozenset({"admin.read", "admin.users", "admin.security", "admin.operations"}),
        is_super_admin=True,
        source="test",
    )

    async def auth_override() -> AuthContext:
        return AuthContext(user_id=USER_ID, method="casdoor")

    async def admin_override() -> AdminContext:
        return admin_context

    async def session_override() -> AsyncGenerator[object]:
        yield object()

    async def fake_idempotency(**kwargs: object):
        assert kwargs["key"] == "idem-test"
        return FakeIdempotencyRepository(), SimpleNamespace(
            id=UUID("33333333-3333-4333-8333-333333333333"),
            state="started",
            response_body=None,
        )

    async def fake_audit(*_args: object, **_kwargs: object) -> None:
        return None

    application.dependency_overrides[account_routes.get_auth_context] = auth_override
    application.dependency_overrides[admin_routes.get_admin_context] = admin_override
    application.dependency_overrides[account_routes._account_repo] = (  # pyright: ignore[reportPrivateUsage]
        FakeAccountRepository
    )
    application.dependency_overrides[admin_routes._account_repo] = (  # pyright: ignore[reportPrivateUsage]
        FakeAccountRepository
    )
    application.dependency_overrides[admin_routes._analytics_repo] = (  # pyright: ignore[reportPrivateUsage]
        FakeAnalyticsRepository
    )
    application.dependency_overrides[admin_routes._plan_repo] = (  # pyright: ignore[reportPrivateUsage]
        FakePlanRepository
    )
    application.dependency_overrides[account_routes._account_center_service] = (  # pyright: ignore[reportPrivateUsage]
        FakeAccountCenterService
    )
    application.dependency_overrides[admin_routes.get_db_session] = session_override
    monkeypatch.setattr(admin_routes, "_idempotency", fake_idempotency)
    monkeypatch.setattr(admin_routes, "_audit", fake_audit)
    return application


@pytest.mark.asyncio
async def test_account_exposes_profile_storage_and_quota(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/account")

    assert response.status_code == 200
    body = response.json()
    assert body["profile"]["avatarUrl"] == "https://example.com/avatar.png"
    assert body["storage"]["effectiveQuotaBytes"] == 1000
    assert body["quotaAccounts"][0]["remaining"] == 9958
    assert body["accessSource"]["method"] == "casdoor"
    assert body["loginProviders"] == []
    assert body["loginProvidersStatus"] == "unavailable"


@pytest.mark.asyncio
async def test_admin_user_detail_and_usage_overview(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get(f"/v1/admin/users/{USER_ID}/detail")
        usage = await client.get("/v1/admin/usage/overview?days=14")

    assert detail.status_code == 200
    assert detail.json()["quotaUsage30d"]["activeDays"] == 4
    assert detail.json()["access"]["mcpAuthorizations"]["active"] == 2
    assert usage.status_code == 200
    assert usage.json()["days"] == 14
    assert usage.json()["mcpToolCalls"] == 90


@pytest.mark.asyncio
async def test_admin_can_create_and_update_dynamic_plan(app: FastAPI) -> None:
    headers = {"Idempotency-Key": "idem-test"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/v1/admin/plans",
            headers=headers,
            json={
                "planKey": "team",
                "name": "Team",
                "entitlements": {"moment.core": True},
                "quotas": {"storage_bytes": 107374182400, "mcp.tool_calls.month": 200000},
                "reason": "launch team plan",
            },
        )
        updated = await client.patch(
            "/v1/admin/plans/plus",
            headers=headers,
            json={
                "expectedVersion": 2,
                "name": "Plus 2026",
                "quotas": {"storage_bytes": 21474836480},
                "reason": "increase storage",
            },
        )

    assert created.status_code == 201
    assert created.json()["key"] == "team"
    assert created.json()["quotas"]["mcp.tool_calls.month"] == 200000
    assert updated.status_code == 200
    assert updated.json()["key"] == "plus"
    assert updated.json()["version"] == 3


@pytest.mark.asyncio
async def test_dynamic_plan_rejects_negative_quota(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/admin/plans",
            headers={"Idempotency-Key": "idem-test"},
            json={"planKey": "bad", "name": "Bad", "quotas": {"api.requests.month": -1}},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_audit_query_is_forwarded(app: FastAPI) -> None:
    repository = FakeAuditRepository()
    app.dependency_overrides[admin_routes._admin_repo] = (  # pyright: ignore[reportPrivateUsage]
        lambda: repository
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/admin/audit-events?query=request-42")

    assert response.status_code == 200
    assert repository.received_query == "request-42"


@pytest.mark.asyncio
async def test_account_center_profile_identity_and_link_routes(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        profile = await client.patch(
            "/v1/account/profile",
            headers={"Idempotency-Key": "profile-idem"},
            json={
                "locale": "en-US",
                "timezone": "Asia/Shanghai",
                "expectedRevision": 1,
            },
        )
        identities = await client.get("/v1/account/identities")
        challenge = await client.post(
            "/v1/account/contact-challenges",
            headers={"Idempotency-Key": "contact-idem"},
            json={
                "kind": "email",
                "destination": "user@example.com",
                "expectedRevision": 1,
            },
        )
        link = await client.post(
            "/v1/account/link-sessions",
            headers={"Idempotency-Key": "link-idem"},
            json={"provider": "github", "returnUri": "https://moment.example/settings"},
        )
        provider_unlink = await client.delete(
            "/v1/account/login-providers/github",
            headers={"Idempotency-Key": "provider-unlink-idem"},
        )
        unlink = await client.post(
            "/v1/account/identities/44444444-4444-4444-8444-444444444444/unlink-preview"
        )
        merge = await client.get(
            "/v1/account/merge-preview?linkSessionId=66666666-6666-4666-8666-666666666666"
        )

    assert profile.status_code == 200
    assert profile.json()["profile"]["locale"] == "en-US"
    assert identities.json()["items"][0]["provider"] == "casdoor"
    assert challenge.status_code == 201
    assert link.status_code == 201
    assert provider_unlink.status_code == 204
    assert unlink.json()["confirmationPhrase"] == "解除绑定"
    assert merge.json()["status"] == "merge_required"


def test_account_and_admin_usage_routes_registered(app: FastAPI) -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/v1/account",
        "/v1/account/profile",
        "/v1/account/avatar",
        "/v1/account/password",
        "/v1/account/identities",
        "/v1/account/login-providers/{provider}",
        "/v1/account/contact-challenges",
        "/v1/account/link-sessions",
        "/v1/account/merge-preview",
        "/v1/account/delete-preview",
        "/v1/account/delete-confirm",
        "/v1/admin/users/{user_id}/detail",
        "/v1/admin/usage/overview",
        "/v1/admin/plans",
        "/v1/admin/plans/{plan_key}",
    }
    assert expected.issubset(paths)
