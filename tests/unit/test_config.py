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
