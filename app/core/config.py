from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MOMENT_ONE_",
        extra="ignore",
        case_sensitive=False,
    )

    env: Literal["development", "test", "staging", "production"] = "development"
    debug: bool = False
    log_level: str = "INFO"
    api_prefix: str = "/v1"
    allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    database_url: str | None = None
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=100)

    casdoor_issuer: HttpUrl | None = None
    casdoor_audience: str | None = None
    casdoor_jwks_url: HttpUrl | None = None

    # 眼镜端 JWT 自签发（QR Binding 授权）
    jwt_private_key_path: str | None = None
    jwt_public_key_path: str | None = None
    jwt_issuer: str = "https://moment-one-api.yuanshuai.fun"
    jwt_audience: str = "moment-one-api"
    access_token_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    refresh_token_ttl_seconds: int = Field(default=7776000, ge=3600, le=31536000)
    binding_code_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    binding_code_length: int = Field(default=24, ge=12, le=64)

    s3_endpoint_url: HttpUrl | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_upload_url_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    s3_download_url_ttl_seconds: int = Field(default=300, ge=60, le=3600)
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1)

    # ---- MCP Server / MCP Apps ----
    # 对外暴露的基础 URL（发现端点、OAuth 回调都基于它拼绝对地址）
    mcp_base_url: str | None = None
    # MCP 工具可用 Scope（RFC 8707 / MCP 授权规范）
    mcp_scopes_supported: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["moments.read", "moments.write", "moments.delete"]
    )
    # Casdoor MCP OAuth 代理客户端（预注册的 Casdoor 应用，confidential client）
    casdoor_mcp_client_id: str | None = None
    casdoor_mcp_client_secret: str | None = None
    casdoor_mcp_redirect_uri: str | None = None
    casdoor_mcp_scope: str = "openid profile email"
    # MCP OAuth 授权码有效期（秒）
    mcp_auth_code_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    # MCP OAuth 客户端注册表 / 授权码存储
    mcp_oauth_clients_enabled: bool = True
    # MCP Apps UI 资源（bookkeeping.html 单文件产物路径，相对仓库根）
    mcp_apps_html_path: str | None = None

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_allowed_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("mcp_scopes_supported", mode="before")
    @classmethod
    def parse_mcp_scopes(cls, value: object) -> object:
        if isinstance(value, str):
            return [scope.strip() for scope in value.split(",") if scope.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
