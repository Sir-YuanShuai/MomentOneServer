"""Moment MCP OAuth 授权服务器（Casdoor 代理）。

MCP Server 作为 OAuth AS 对外提供（RFC 9728 / RFC 8414 发现 + RFC 7591 DCR +
PKCE 授权码），浏览器跳转 Casdoor 登录后，由本服务用**预注册的 Casdoor 客户端**
（confidential client，env 配置）换取 Casdoor token，再签发 **MomentOne 自签 RS256
token**（与眼镜端 QR Binding 同一套 JwtIssuer 验签）——两种授权形态的 token 统一验证。

流程（docs/roadmap/MCP_APPS_PLAN.md §3.1 / §9 风险 P1 缓解）：
1. authorize：校验 DCR 客户端 → 创建 casdoor_txn（含客户端 PKCE challenge + 我方
   对 Casdoor 的 PKCE verifier，dual-PKCE）→ 302 跳转 Casdoor
2. callback：Casdoor code → token 交换 → JWKS 验签 + 本地用户同步 → 签发我方授权码
   （绑定用户与 scope）→ 302 回客户端 redirect_uri
3. token：客户端用授权码 + PKCE verifier 换我方 RS256 access_token + refresh_token
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.user_repository import resolve_user_id
from app.infrastructure.identity.casdoor import CasdoorTokenVerifier
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.mcp.scope import ALL_SCOPES
from app.modules.mcp_oauth.repositories import (
    McpAuthCodeRepository,
    McpAuthorizationRepository,
    McpClientRepository,
)

GRANT_AUTHORIZATION_CODE = "authorization_code"
GRANT_REFRESH_TOKEN = "refresh_token"
GRANT_QR_BINDING = "urn:momentone:oauth:grant-type:qr-binding"

CODE_KIND_CASDOOR_TXN = "casdoor_txn"
CODE_KIND_AUTH_CODE = "auth_code"

# 默认 scope：对齐 Web 端 QR Binding 的默认授权（DEVICE_BINDING.md §4.1），
# 读写都可用；moments.delete 仍需客户端显式请求（高危操作）。
DEFAULT_SCOPE = "moments.read moments.write"


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def derive_code_challenge(verifier: str) -> str:
    """S256 PKCE challenge。"""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _verify_pkce(verifier: str, challenge: str) -> bool:
    return bool(verifier) and derive_code_challenge(verifier) == challenge


class CasdoorProxyClient:
    """预注册 Casdoor 客户端的授权/换码封装（confidential client）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _issuer(self) -> str:
        if not self._settings.casdoor_issuer:
            raise ApplicationError(
                code="MCP_OAUTH_NOT_CONFIGURED",
                message="Casdoor 未配置，无法完成 MCP OAuth 授权。",
                status_code=500,
            )
        return str(self._settings.casdoor_issuer).rstrip("/")

    def _client_id(self) -> str:
        if not self._settings.casdoor_mcp_client_id:
            raise ApplicationError(
                code="MCP_OAUTH_NOT_CONFIGURED",
                message="casdoor_mcp_client_id 未配置。",
                status_code=500,
            )
        return self._settings.casdoor_mcp_client_id

    def _client_secret(self) -> str | None:
        """可选：复用前端 public client 时无 secret（靠 PKCE 保证安全）。"""
        return self._settings.casdoor_mcp_client_secret or None

    def authorize_url(
        self,
        *,
        state: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> str:
        params = {
            "client_id": self._client_id(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self._settings.casdoor_mcp_scope,
            "state": state,
            "code_challenge": derive_code_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        return f"{self._issuer()}/login/oauth/authorize?{urlencode(params)}"

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict:
        """用 Casdoor 授权码换 token（access_token + id_token）。

        client_secret 可选：复用前端 public client 时省略，靠 PKCE 保证安全。
        """
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._client_id(),
            "code_verifier": code_verifier,
        }
        secret = self._client_secret()
        if secret:
            data["client_secret"] = secret
        url = f"{self._issuer()}/api/login/oauth/access_token"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, data=data)
        except httpx.HTTPError as exc:
            raise ApplicationError(
                code="OAUTH_UPSTREAM_ERROR",
                message="Casdoor token 交换失败，请稍后重试。",
                status_code=502,
            ) from exc
        if resp.status_code != 200:
            raise ApplicationError(
                code="OAUTH_UPSTREAM_ERROR",
                message="Casdoor 拒绝了授权码交换。",
                status_code=502,
                details={"statusCode": resp.status_code},
            )
        return resp.json()


@dataclass(frozen=True, slots=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    scope: str
    binding_id: str | None = None


class MomentOAuthService:
    """MCP OAuth 授权服务器服务层（DCR + authorize + callback + token）。"""

    def __init__(
        self,
        *,
        settings: Settings,
        jwt_issuer: JwtIssuer,
        session: AsyncSession,
    ) -> None:
        self._settings = settings
        self._jwt_issuer = jwt_issuer
        self._session = session
        self._clients = McpClientRepository(session)
        self._codes = McpAuthCodeRepository(session)
        self._authorizations = McpAuthorizationRepository(session)
        self._casdoor = CasdoorProxyClient(settings)
        self._casdoor_verifier = CasdoorTokenVerifier(settings)

    # ------------------------------------------------------------------
    # DCR（RFC 7591）
    # ------------------------------------------------------------------

    def _validate_scopes(self, scope_str: str | None) -> tuple[str, ...]:
        """请求的 scope 必须是支持的 scope 子集；缺省为 moments.read。"""
        requested = [s for s in (scope_str or DEFAULT_SCOPE).split() if s]
        supported = set(self._settings.mcp_scopes_supported) | set(ALL_SCOPES)
        unknown = [s for s in requested if s not in supported]
        if unknown:
            raise ApplicationError(
                code="INVALID_SCOPE",
                message=f"不支持的 scope：{', '.join(unknown)}",
                status_code=400,
                details={"scopes": list(self._settings.mcp_scopes_supported)},
            )
        return tuple(requested)

    async def register_client(self, registration: dict) -> dict:
        """RFC 7591 动态客户端注册。公共客户端（PKCE 强制，无 client_secret）。"""
        redirect_uris = registration.get("redirect_uris") or registration.get("redirect_uris", [])
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise ApplicationError(
                code="INVALID_CLIENT_METADATA",
                message="redirect_uris 至少需要一个绝对 URL。",
                status_code=400,
            )
        if not all(
            isinstance(u, str) and u.startswith(("http://", "https://")) for u in redirect_uris
        ):
            raise ApplicationError(
                code="INVALID_CLIENT_METADATA",
                message="redirect_uris 必须是绝对 URL。",
                status_code=400,
            )

        scope_str = registration.get("scope") or DEFAULT_SCOPE
        scopes = self._validate_scopes(scope_str)

        client_id = f"momentone-{secrets.token_hex(16)}"
        client = await self._clients.create(
            client_id=client_id,
            client_name=str(registration.get("client_name") or "MCP Client"),
            redirect_uris=redirect_uris,
            scope=" ".join(scopes),
            grant_types=registration.get("grant_types")
            or [GRANT_AUTHORIZATION_CODE, GRANT_REFRESH_TOKEN],
            token_endpoint_auth_method="none",
        )

        base = self._settings.mcp_base_url or "http://127.0.0.1:8000"
        return {
            "client_id": client.client_id,
            "client_id_issued_at": int(client.created_at.timestamp()),
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "scope": client.scope,
            "grant_types": client.grant_types,
            "token_endpoint_auth_method": "none",
            "registration_access_token": None,
            "registration_client_uri": f"{base}/oauth/register/{client.client_id}",
        }

    # ------------------------------------------------------------------
    # authorize（浏览器跳转 Casdoor）
    # ------------------------------------------------------------------

    async def start_authorize(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        scope: str | None,
        state: str | None,
        code_challenge: str | None,
        code_challenge_method: str | None,
        resource: str | None = None,
    ) -> str:
        """校验客户端与参数，创建 casdoor_txn，返回 Casdoor 授权页 URL。"""
        if code_challenge_method not in (None, "S256"):
            raise ApplicationError(
                code="INVALID_REQUEST",
                message="仅支持 code_challenge_method=S256。",
                status_code=400,
            )
        if not code_challenge:
            raise ApplicationError(
                code="INVALID_REQUEST",
                message="PKCE 必需：缺少 code_challenge。",
                status_code=400,
            )

        client = await self._clients.get_by_client_id(client_id)
        if client is None or client.status != "active":
            raise ApplicationError(
                code="INVALID_CLIENT",
                message="未知或已停用的 client_id。",
                status_code=401,
            )
        if redirect_uri not in (client.redirect_uris or []):
            raise ApplicationError(
                code="INVALID_REDIRECT_URI",
                message="redirect_uri 未注册。",
                status_code=400,
            )

        scopes = self._validate_scopes(scope)
        casdoor_state = secrets.token_urlsafe(32)
        casdoor_verifier = generate_code_verifier()

        now = datetime.now(UTC)
        await self._codes.create(
            code=casdoor_state,
            kind=CODE_KIND_CASDOOR_TXN,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=" ".join(scopes),
            state=state,
            code_challenge=code_challenge,
            casdoor_code_verifier=casdoor_verifier,
            resource=resource,
            user_id=None,
            expires_at=now + timedelta(seconds=self._settings.mcp_auth_code_ttl_seconds),
        )

        return self._casdoor.authorize_url(
            state=casdoor_state,
            code_verifier=casdoor_verifier,
            redirect_uri=self._casdoor_callback_uri(),
        )

    def _casdoor_callback_uri(self) -> str:
        base = self._settings.mcp_base_url or "http://127.0.0.1:8000"
        if self._settings.casdoor_mcp_redirect_uri:
            return self._settings.casdoor_mcp_redirect_uri
        return f"{base}/oauth/callback"

    # ------------------------------------------------------------------
    # callback（Casdoor → 我方授权码）
    # ------------------------------------------------------------------

    async def handle_casdoor_callback(
        self,
        *,
        code: str,
        state: str,
        error: str | None = None,
    ) -> str:
        """处理 Casdoor 回调：换 token → 识别用户 → 签发我方授权码 → 返回客户端跳转 URL。

        Casdoor 拒绝（error）时：若有对应事务，302 回客户端 redirect_uri 带 error
        （RFC 6749 §4.1.2.1），否则抛 OAUTH_DENIED。
        """
        txn = await self._codes.get_by_code(state)

        if error:
            if txn is not None and txn.redirect_uri:
                sep = "&" if "?" in txn.redirect_uri else "?"
                return f"{txn.redirect_uri}{sep}error={error}"
            raise ApplicationError(
                code="OAUTH_DENIED",
                message=f"用户在 Casdoor 拒绝了授权：{error}",
                status_code=400,
                details={"error": error},
            )

        if (
            txn is None
            or txn.kind != CODE_KIND_CASDOOR_TXN
            or txn.status != "pending"
            or datetime.now(UTC) > txn.expires_at
        ):
            raise ApplicationError(
                code="INVALID_GRANT",
                message="授权会话已失效，请重新发起授权。",
                status_code=400,
            )

        # 与 Casdoor 换 token（dual-PKCE：我方 verifier）
        casdoor_tokens = await self._casdoor.exchange_code(
            code=code,
            code_verifier=txn.casdoor_code_verifier or "",
            redirect_uri=self._casdoor_callback_uri(),
        )
        casdoor_access = casdoor_tokens.get("access_token")
        if not casdoor_access:
            raise ApplicationError(
                code="OAUTH_UPSTREAM_ERROR",
                message="Casdoor 未返回 access_token。",
                status_code=502,
            )

        # JWKS 验签 + 本地用户 upsert（与 Web 登录同一套身份同步）
        user_id = await resolve_user_id(self._session, self._casdoor_verifier, casdoor_access)

        # 签发我方授权码（客户端用 code + PKCE verifier 换 token）
        auth_code = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        await self._codes.create(
            code=auth_code,
            kind=CODE_KIND_AUTH_CODE,
            client_id=txn.client_id,
            redirect_uri=txn.redirect_uri,
            scope=txn.scope,
            state=txn.state,
            code_challenge=txn.code_challenge,
            casdoor_code_verifier=None,
            resource=txn.resource,
            user_id=user_id,
            expires_at=now + timedelta(seconds=self._settings.mcp_auth_code_ttl_seconds),
        )
        # 消费 casdoor_txn
        await self._codes.mark_consumed(code_id=txn.id)

        # 记录授权关系（Web 端可查看/调整/撤销）
        client = await self._clients.get_by_client_id(txn.client_id)
        await self._authorizations.upsert(
            user_id=user_id,
            client_id=txn.client_id,
            client_name=client.client_name if client else None,
            scope=txn.scope or DEFAULT_SCOPE,
        )

        # 302 回客户端 redirect_uri
        sep = "&" if "?" in (txn.redirect_uri or "") else "?"
        url = f"{txn.redirect_uri}{sep}code={auth_code}"
        if txn.state:
            url += f"&state={txn.state}"
        return url

    # ------------------------------------------------------------------
    # token（授权码换 token / 刷新）
    # ------------------------------------------------------------------

    async def exchange_auth_code(
        self,
        *,
        client_id: str,
        code: str,
        code_verifier: str,
        redirect_uri: str | None,
    ) -> TokenResponse:
        """客户端用授权码 + PKCE verifier 换我方 RS256 token。"""
        record = await self._codes.get_by_code(code)
        if (
            record is None
            or record.kind != CODE_KIND_AUTH_CODE
            or record.status != "pending"
            or datetime.now(UTC) > record.expires_at
        ):
            raise ApplicationError(
                code="INVALID_GRANT",
                message="授权码无效或已过期。",
                status_code=400,
            )
        if record.client_id != client_id:
            raise ApplicationError(
                code="INVALID_GRANT",
                message="授权码不属于该客户端。",
                status_code=400,
            )
        if redirect_uri and record.redirect_uri != redirect_uri:
            raise ApplicationError(
                code="INVALID_GRANT",
                message="redirect_uri 与授权时不一致。",
                status_code=400,
            )
        if record.user_id is None:
            raise ApplicationError(
                code="INVALID_GRANT",
                message="授权码缺少用户绑定。",
                status_code=400,
            )
        if not _verify_pkce(code_verifier, record.code_challenge or ""):
            raise ApplicationError(
                code="INVALID_GRANT",
                message="PKCE code_verifier 校验失败。",
                status_code=400,
            )

        scopes = tuple((record.scope or DEFAULT_SCOPE).split())
        access_token, expires_in = self._jwt_issuer.issue_mcp_access_token(
            user_id=record.user_id,
            scope=scopes,
            client_id=client_id,
            resource=record.resource,
        )
        refresh_token = self._jwt_issuer.issue_mcp_refresh_token(
            user_id=record.user_id,
            scope=scopes,
            client_id=client_id,
            resource=record.resource,
        )
        await self._codes.mark_consumed(code_id=record.id)

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="Bearer",
            expires_in=expires_in,
            scope=" ".join(scopes),
        )

    async def refresh_mcp_token(self, refresh_token: str) -> TokenResponse:
        """MCP OAuth refresh_token 刷新（不滚动，30 天硬上限，与眼镜端一致）。"""
        payload = self._jwt_issuer.verify_refresh_token(refresh_token)
        if payload.get("grant") != "authorization_code":
            raise ApplicationError(
                code="INVALID_GRANT",
                message="refresh_token 不是 MCP OAuth 类型。",
                status_code=400,
            )
        user_id = UUID(payload["sub"])
        scope = tuple((payload.get("scope") or DEFAULT_SCOPE).split())
        client_id = str(payload.get("client_id") or "")
        access_token, expires_in = self._jwt_issuer.issue_mcp_access_token(
            user_id=user_id,
            scope=scope,
            client_id=client_id,
            resource=payload.get("aud"),
        )
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="Bearer",
            expires_in=expires_in,
            scope=" ".join(scope),
        )


__all__ = [
    "GRANT_AUTHORIZATION_CODE",
    "GRANT_REFRESH_TOKEN",
    "GRANT_QR_BINDING",
    "MomentOAuthService",
    "TokenResponse",
]
