"""RS256 JWT 签发器，用于眼镜端 access_token 和 refresh_token。

眼镜端通过 QR Binding（Extension Grant）获得 token，token 的 iss 是
MomentOneServer 自身（区别于 Casdoor）。未来 MCP Server 验签时需同时
支持 Casdoor 和 MomentOneServer 两种 issuer，按 iss 路由到对应公钥。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import jwt

from app.core.config import Settings
from app.core.errors import ApplicationError


class JwtIssuer:
    """RS256 JWT 签发器。私钥/公钥从 PEM 文件加载，启动时读入内存。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._private_key: str | None = None
        self._public_key: str | None = None

    def _ensure_keys(self) -> tuple[str, str]:
        """加载并返回 (private_key, public_key)。已加载则直接返回缓存。"""
        if self._private_key is not None and self._public_key is not None:
            return self._private_key, self._public_key
        private_path = self._settings.jwt_private_key_path
        public_path = self._settings.jwt_public_key_path
        if not private_path or not public_path:
            raise ApplicationError(
                code="JWT_NOT_CONFIGURED",
                message="JWT 私钥/公钥路径未配置。",
                status_code=500,
            )
        self._private_key = Path(private_path).read_text(encoding="utf-8")
        self._public_key = Path(public_path).read_text(encoding="utf-8")
        return self._private_key, self._public_key

    def issue_access_token(
        self,
        *,
        binding_id: UUID,
        user_id: UUID,
        device_id: str,
        scope: tuple[str, ...],
    ) -> tuple[str, int]:
        """签发 access_token，返回 (token, expires_in_seconds)。"""
        private_key, _ = self._ensure_keys()
        now = datetime.now(UTC)
        expires_in = self._settings.access_token_ttl_seconds
        payload = {
            "iss": self._settings.jwt_issuer,
            "sub": str(user_id),
            "aud": self._settings.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
            "binding_id": str(binding_id),
            "device_id": device_id,
            "scope": " ".join(scope),
            "token_type": "access",
        }
        token = jwt.encode(payload, private_key, algorithm="RS256")
        return token, expires_in

    def issue_refresh_token(
        self,
        *,
        binding_id: UUID,
        user_id: UUID,
        device_id: str,
        scope: tuple[str, ...],
    ) -> str:
        """签发 refresh_token（90 天滚动续期）。"""
        private_key, _ = self._ensure_keys()
        now = datetime.now(UTC)
        expires_in = self._settings.refresh_token_ttl_seconds
        payload = {
            "iss": self._settings.jwt_issuer,
            "sub": str(user_id),
            "aud": self._settings.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
            "binding_id": str(binding_id),
            "device_id": device_id,
            "scope": " ".join(scope),
            "token_type": "refresh",
        }
        return jwt.encode(payload, private_key, algorithm="RS256")

    def verify_refresh_token(self, token: str) -> dict:
        """验签 refresh_token，返回 payload。失败抛 ApplicationError。"""
        _, public_key = self._ensure_keys()
        try:
            payload = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=self._settings.jwt_audience,
                issuer=self._settings.jwt_issuer,
            )
        except jwt.PyJWTError as exc:
            raise ApplicationError(
                code="REFRESH_TOKEN_INVALID",
                message="refresh_token 验签失败。",
                status_code=401,
            ) from exc
        if payload.get("token_type") != "refresh":
            raise ApplicationError(
                code="REFRESH_TOKEN_INVALID",
                message="token 不是 refresh_token。",
                status_code=401,
            )
        return payload

    def verify_access_token(self, token: str) -> dict:
        """验签眼镜端 access_token，返回 payload。失败抛 ApplicationError。

        用于业务 API（如 /v1/moments）接受眼镜端 JWT 鉴权。
        校验 RS256 签名 + iss + aud + exp + token_type=access。
        """
        _, public_key = self._ensure_keys()
        try:
            payload = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=self._settings.jwt_audience,
                issuer=self._settings.jwt_issuer,
            )
        except jwt.PyJWTError as exc:
            raise ApplicationError(
                code="TOKEN_INVALID",
                message="access_token 验签失败。",
                status_code=401,
            ) from exc
        if payload.get("token_type") != "access":
            raise ApplicationError(
                code="TOKEN_INVALID",
                message="token 不是 access_token。",
                status_code=401,
            )
        return payload

    @property
    def issuer(self) -> str:
        """当前签发方 issuer，用于路由判断。"""
        return self._settings.jwt_issuer
