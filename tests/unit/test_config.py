import pytest
from app.core.config import Settings
from pydantic import HttpUrl, ValidationError


def test_allowed_origins_accepts_comma_separated_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MOMENT_ONE_ALLOWED_ORIGINS",
        "http://localhost:5173, https://app.example.com",
    )

    settings = Settings()

    assert settings.allowed_origins == [
        "http://localhost:5173",
        "https://app.example.com",
    ]


def test_casdoor_tenant_contract_requires_organization() -> None:
    with pytest.raises(ValidationError, match="casdoor_organization"):
        Settings(
            casdoor_issuer=HttpUrl("https://identity.example.com"),
            casdoor_audience="moment-one-client",
            casdoor_jwks_url=HttpUrl("https://identity.example.com/.well-known/jwks"),
            casdoor_organization=None,
            casdoor_application="MomentOne",
            casdoor_application_id="admin/MomentOne",
        )


def test_casdoor_tenant_contract_rejects_different_client_application() -> None:
    with pytest.raises(ValidationError, match="casdoor_mcp_client_id must equal"):
        Settings(
            casdoor_issuer=HttpUrl("https://identity.example.com"),
            casdoor_audience="moment-one-client",
            casdoor_jwks_url=HttpUrl("https://identity.example.com/.well-known/jwks"),
            casdoor_organization="example.org",
            casdoor_application="MomentOne",
            casdoor_application_id="admin/MomentOne",
            casdoor_mcp_client_id="another-application-client",
        )


def test_web_push_enabled_requires_complete_secret_set() -> None:
    with pytest.raises(ValidationError, match="web_push_vapid_private_key"):
        Settings(
            web_push_enabled=True,
            web_push_vapid_public_key="public",
            web_push_vapid_private_key=None,
            web_push_vapid_subject=None,
            web_push_subscription_encryption_key=None,
        )


def test_web_push_endpoint_hosts_accept_comma_separated_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MOMENT_ONE_WEB_PUSH_ALLOWED_ENDPOINT_HOSTS",
        "fcm.googleapis.com, web.push.apple.com",
    )
    settings = Settings()
    assert settings.web_push_allowed_endpoint_hosts == [
        "fcm.googleapis.com",
        "web.push.apple.com",
    ]


def test_gpt_action_oauth_requires_client_id_and_secret_together() -> None:
    with pytest.raises(ValidationError, match="gpt_action_client_id"):
        Settings(env="test", gpt_action_client_id="chatgpt-action")

    configured = Settings(
        env="test",
        gpt_action_client_id="chatgpt-action",
        gpt_action_client_secret="secret-value",
    )
    assert configured.gpt_action_client_id == "chatgpt-action"
