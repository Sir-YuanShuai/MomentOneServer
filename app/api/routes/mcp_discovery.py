"""MCP OAuth 发现端点（RFC 9728 Protected Resource Metadata + RFC 8414 AS Metadata）。

- `GET /.well-known/oauth-protected-resource`（根路径 + `/mcp` 子路径，ChatGPT 要求 path-aware）
- `GET /.well-known/oauth-authorization-server`（同上）

MCP Host 收到 401（WWW-Authenticate 带 resource_metadata）后，据此发现授权服务器
并完成 DCR + PKCE 授权（见 docs/roadmap/MCP_APPS_PLAN.md §3.1）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings

router = APIRouter(tags=["mcp-discovery"])


def _base_url(settings: Settings) -> str:
    return (settings.mcp_base_url or "http://127.0.0.1:8000").rstrip("/")


def _protected_resource(settings: Settings) -> dict:
    base = _base_url(settings)
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
    }


def _as_metadata(settings: Settings) -> dict:
    base = _base_url(settings)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": list(settings.mcp_scopes_supported),
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
            "urn:momentone:oauth:grant-type:qr-binding",
        ],
        "token_endpoint_auth_methods_supported": ["none"],
        "response_types_supported": ["code"],
    }


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_root(  # pyright: ignore[reportUnusedFunction]
    settings: Settings = Depends(get_settings),
) -> dict:
    return _protected_resource(settings)


@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_mcp(  # pyright: ignore[reportUnusedFunction]
    settings: Settings = Depends(get_settings),
) -> dict:
    return _protected_resource(settings)


@router.get("/.well-known/oauth-authorization-server")
async def as_metadata_root(  # pyright: ignore[reportUnusedFunction]
    settings: Settings = Depends(get_settings),
) -> dict:
    return _as_metadata(settings)


@router.get("/.well-known/oauth-authorization-server/mcp")
async def as_metadata_mcp(  # pyright: ignore[reportUnusedFunction]
    settings: Settings = Depends(get_settings),
) -> dict:
    return _as_metadata(settings)


__all__ = ["router"]
