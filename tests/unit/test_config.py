import pytest
from app.core.config import Settings


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
