from app.api.routes.moments import (
    CreateMomentFromOpenAIRequest,
    validate_openai_download_url,
)
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.modules.mcp_oauth.service import MomentOAuthService


def test_openai_file_refs_are_part_of_action_schema() -> None:
    schema = CreateMomentFromOpenAIRequest.model_json_schema()
    field = schema["properties"]["openaiFileIdRefs"]
    assert field["maxItems"] == 10
    assert "ChatGPT" in field["description"]


def test_openai_download_url_accepts_configured_https_host() -> None:
    settings = Settings(env="test", openai_attachment_allowed_hosts=["files.oaiusercontent.com"])
    validate_openai_download_url(
        "https://files.oaiusercontent.com/file-123?signature=short-lived", settings
    )


def test_openai_download_url_rejects_arbitrary_and_lookalike_hosts() -> None:
    settings = Settings(env="test", openai_attachment_allowed_hosts=["files.oaiusercontent.com"])
    for url in (
        "http://files.oaiusercontent.com/file-123",
        "https://files.oaiusercontent.com.evil.test/file-123",
        "https://127.0.0.1/internal",
        "https://user@files.oaiusercontent.com/file-123",
    ):
        try:
            validate_openai_download_url(url, settings)
        except ApplicationError as exc:
            assert exc.code == "UNTRUSTED_ATTACHMENT_SOURCE"
        else:  # pragma: no cover
            raise AssertionError(f"URL should have been rejected: {url}")


def test_gpt_action_redirect_uri_is_limited_to_chatgpt_callback() -> None:
    assert MomentOAuthService.is_gpt_action_redirect_uri(
        "https://chatgpt.com/aip/g-moment-one/oauth/callback"
    )
    assert MomentOAuthService.is_gpt_action_redirect_uri(
        "https://chat.openai.com/aip/g-moment-one/oauth/callback"
    )
    assert not MomentOAuthService.is_gpt_action_redirect_uri(
        "https://chatgpt.com.evil.test/aip/g-moment-one/oauth/callback"
    )
    assert not MomentOAuthService.is_gpt_action_redirect_uri(
        "https://chatgpt.com/aip/not-a-gpt/oauth/callback"
    )
