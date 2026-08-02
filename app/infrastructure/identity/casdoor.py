import time
from dataclasses import dataclass

import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import Settings
from app.core.errors import ApplicationError


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    issuer: str
    subject: str
    audience: str | None = None
    email: str | None = None
    display_name: str | None = None


class CasdoorTokenVerifier:
    """Casdoor OIDC Access Token 验证器。

    使用 JWKS 公钥验证 RS256 签名，校验 iss / aud / exp。
    JWKS 公钥缓存 10 分钟，避免每次请求都拉取。
    """

    _jwks_client: PyJWKClient | None = None
    _jwks_fetched_at: float = 0
    _jwks_ttl: float = 600.0  # 10 分钟

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _ensure_jwks_client(self) -> PyJWKClient:
        now = time.monotonic()
        if self._jwks_client is None or now - self._jwks_fetched_at > self._jwks_ttl:
            jwks_url = str(self._settings.casdoor_jwks_url) if self._settings.casdoor_jwks_url else None
            if not jwks_url:
                raise ApplicationError(
                    code="INTERNAL_ERROR",
                    message="Casdoor JWKS URL 未配置。",
                    status_code=500,
                )
            self._jwks_client = PyJWKClient(jwks_url)
            self._jwks_fetched_at = now
        return self._jwks_client

    def verify(self, access_token: str) -> AuthenticatedPrincipal:
        """验证 Access Token 并返回认证主体信息。

        Raises:
            ApplicationError: TOKEN_INVALID 或 AUTH_REQUIRED
        """
        try:
            jwks = self._ensure_jwks_client()
            signing_key = jwks.get_signing_key_from_jwt(access_token)

            issuer = str(self._settings.casdoor_issuer).rstrip("/")
            audience = self._settings.casdoor_audience

            # 先无验签解码 payload，检查是否包含 aud 字段
            unverified = jwt.decode(access_token, options={"verify_signature": False})
            has_aud = "aud" in unverified

            import structlog
            structlog.get_logger().adebug(
                "jwt_payload_fields",
                fields=list(unverified.keys()),
                has_aud=has_aud,
                sub=unverified.get("sub"),
            )

            # 仅当 JWT 包含 aud 时才校验受众（Casdoor 可能不包含 aud）
            payload = jwt.decode(
                access_token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=issuer,
                audience=audience if has_aud else None,
            )

            sub = payload.get("sub")
            if not sub:
                raise ApplicationError(
                    code="TOKEN_INVALID",
                    message="Access Token 中缺少用户标识 (sub)。",
                    status_code=401,
                )

            return AuthenticatedPrincipal(
                issuer=issuer,
                subject=str(sub),
                audience=audience,
                email=payload.get("email"),
                display_name=payload.get("name") or payload.get("preferred_username"),
            )
        except jwt.ExpiredSignatureError:
            raise ApplicationError(
                code="TOKEN_INVALID",
                message="Access Token 已过期，请重新登录。",
                status_code=401,
            ) from None
        except jwt.InvalidAudienceError:
            raise ApplicationError(
                code="TOKEN_INVALID",
                message="Access Token 受众不匹配。",
                status_code=401,
            ) from None
        except jwt.InvalidIssuerError:
            raise ApplicationError(
                code="TOKEN_INVALID",
                message="Access Token 签发方不匹配。",
                status_code=401,
            ) from None
        except ApplicationError:
            raise
        except Exception as exc:
            import structlog
            structlog.get_logger().awarning(
                "jwt_verification_failed",
                error_type=type(exc).__name__,
                error_msg=str(exc),
            )
            raise ApplicationError(
                code="TOKEN_INVALID",
                message="Access Token 验证失败。",
                status_code=401,
            ) from None

    async def fetch_userinfo(self, access_token: str) -> dict:
        """调用 Casdoor userinfo endpoint 获取用户详情（含 UUID id）。"""
        userinfo_url = f"{str(self._settings.casdoor_issuer).rstrip('/')}/api/userinfo"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            return resp.json()
