"""MCP OAuth 发现端点（RFC 9728 Protected Resource Metadata + RFC 8414 AS Metadata）。

- `GET /.well-known/oauth-protected-resource`（根路径 + `/mcp` 子路径，ChatGPT 要求 path-aware）
- `GET /.well-known/oauth-authorization-server`（同上）
- `GET /.well-known/openid-configuration`（OIDC Discovery，OpenAI 的 OAuth 客户端会校验）
- `GET /.well-known/jwks.json`（RS256 公钥，供客户端验签/完整性校验）

MCP Host 收到 401（WWW-Authenticate 带 resource_metadata）后，据此发现授权服务器
并完成 DCR + PKCE 授权（见 docs/roadmap/MCP_APPS_PLAN.md §3.1）。
"""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
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


def _oidc_configuration(settings: Settings) -> dict:
    """OIDC Discovery（OpenID Connect 1.0 §4，OpenAI 的 OAuth 客户端会请求并校验）。"""
    base = _base_url(settings)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "jwks_uri": f"{base}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": list(settings.mcp_scopes_supported),
        "claims_supported": ["sub", "iss", "aud", "exp", "iat"],
    }


def _jwks(settings: Settings) -> dict:
    """从 JWT 公钥（RS256）生成 JWKS（RFC 7517）。仅读取公钥，不触碰私钥。"""
    if not settings.jwt_public_key_path:
        return {"keys": []}
    p = Path(settings.jwt_public_key_path)
    if not p.is_file():
        return {"keys": []}
    public_key = serialization.load_pem_public_key(p.read_bytes())
    if not isinstance(public_key, rsa.RSAPublicKey):
        return {"keys": []}
    numbers = public_key.public_numbers()
    n = numbers.n
    e = numbers.e
    modulus = (
        base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()
    )
    exponent = (
        base64.urlsafe_b64encode(e.to_bytes((e.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()
    )
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "n": modulus,
                "e": exponent,
            }
        ]
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


@router.get("/.well-known/openid-configuration")
async def oidc_configuration(  # pyright: ignore[reportUnusedFunction]
    settings: Settings = Depends(get_settings),
) -> dict:
    """OIDC Discovery：OpenAI 的 OAuth 客户端在 token 交换后校验，缺失会导致连接失败。"""
    return _oidc_configuration(settings)


@router.get("/.well-known/jwks.json")
async def jwks(  # pyright: ignore[reportUnusedFunction]
    settings: Settings = Depends(get_settings),
) -> dict:
    """JWKS（仅公钥，RS256）。"""
    return _jwks(settings)


__all__ = ["router"]
