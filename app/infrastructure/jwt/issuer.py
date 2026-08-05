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

# JWKS kid：MCP token 的 JWT header 与 /.well-known/jwks.json 共用
JWT_KID = "moment-one-rs256"


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

    def issue_mcp_access_token(
        self,
        *,
        user_id: UUID,
        scope: tuple[str, ...],
        client_id: str,
        resource: str | None = None,
    ) -> tuple[str, int]:
        """签发 MCP OAuth access_token（Authorization Code + PKCE 路径）。

        与眼镜端 token 同一套 RS256 签名/验签，但无 binding_id：
        - claims 增加 `grant=authorization_code` + `client_id`
        - aud 遵循 RFC 8707：等于客户端请求的 resource（如 https://api/mcp），
          供严格 Host（ChatGPT）校验 token 用途
        - JWT header 带 kid，与 /.well-known/jwks.json 匹配
        """
        private_key, _ = self._ensure_keys()
        now = datetime.now(UTC)
        expires_in = self._settings.access_token_ttl_seconds
        payload = {
            "iss": self._settings.jwt_issuer,
            "sub": str(user_id),
            "aud": resource or self._settings.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
            "scope": " ".join(scope),
            "token_type": "access",
            "grant": "authorization_code",
            "client_id": client_id,
        }
        token = jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": JWT_KID})
        return token, expires_in

    def issue_mcp_refresh_token(
        self,
        *,
        user_id: UUID,
        scope: tuple[str, ...],
        client_id: str,
        resource: str | None = None,
    ) -> str:
        """签发 MCP OAuth refresh_token（不滚动，与眼镜端 refresh_token 一致：30 天硬上限）。"""
        private_key, _ = self._ensure_keys()
        now = datetime.now(UTC)
        expires_in = self._settings.refresh_token_ttl_seconds
        payload = {
            "iss": self._settings.jwt_issuer,
            "sub": str(user_id),
            "aud": resource or self._settings.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
            "scope": " ".join(scope),
            "token_type": "refresh",
            "grant": "authorization_code",
            "client_id": client_id,
        }
        return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": JWT_KID})

    def verify_refresh_token(self, token: str) -> dict:
        """验签 refresh_token，返回 payload。失败抛 ApplicationError。

        aud 按 grant 区分：MCP refresh（resource）与眼镜端 refresh（jwt_audience）。
        """
        _, public_key = self._ensure_keys()
        try:
            payload = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                issuer=self._settings.jwt_issuer,
                options={"verify_aud": False},  # aud 在下方按 grant 手动校验（PyJWT 要求显式关闭）
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
        aud = payload.get("aud")
        if payload.get("grant") == "authorization_code":
            # 接受 resource（RFC 8707）或 jwt_audience（客户端未带 resource 时）
            if aud not in (self._mcp_resource_url(), self._settings.jwt_audience):
                raise ApplicationError(
                    code="REFRESH_TOKEN_INVALID",
                    message="refresh_token 受众不匹配。",
                    status_code=401,
                )
        elif aud != self._settings.jwt_audience:
            raise ApplicationError(
                code="REFRESH_TOKEN_INVALID",
                message="refresh_token 受众不匹配。",
                status_code=401,
            )
        return payload

    def verify_access_token(self, token: str) -> dict:
        """验签 Server 自签 access_token，返回 payload。失败抛 ApplicationError。

        aud 校验按 grant 区分（RFC 8707）：
        - MCP OAuth token（grant=authorization_code）：aud = 客户端请求的 resource
          （settings.mcp_base_url + /mcp），由验签方二次确认
        - 眼镜端 token：aud = settings.jwt_audience
        """
        _, public_key = self._ensure_keys()
        try:
            payload = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                issuer=self._settings.jwt_issuer,
                options={"verify_aud": False},  # aud 在下方按 grant 手动校验
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
        aud = payload.get("aud")
        if payload.get("grant") == "authorization_code":
            # 接受 resource（RFC 8707）或 jwt_audience（客户端未带 resource 时）
            if aud not in (self._mcp_resource_url(), self._settings.jwt_audience):
                raise ApplicationError(
                    code="TOKEN_INVALID",
                    message="MCP token 受众不匹配。",
                    status_code=401,
                )
        elif aud != self._settings.jwt_audience:
            raise ApplicationError(
                code="TOKEN_INVALID",
                message="access_token 受众不匹配。",
                status_code=401,
            )
        return payload

    def _mcp_resource_url(self) -> str:
        base = (self._settings.mcp_base_url or "http://127.0.0.1:8000").rstrip("/")
        return f"{base}/mcp"

    @property
    def issuer(self) -> str:
        """当前签发方 issuer，用于路由判断。"""
        return self._settings.jwt_issuer
