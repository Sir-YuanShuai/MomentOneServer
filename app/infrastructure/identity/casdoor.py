import time
from dataclasses import dataclass

import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import Settings
from app.core.errors import ApplicationError


def _claim_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            for key in ("name", "id", "displayName"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    names.append(candidate.strip())
                    break
    return tuple(dict.fromkeys(names))


def _claim_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"}


def _claim_optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes"}:
            return True
        if normalized in {"0", "false", "no"}:
            return False
    return None


def _first_claim_bool(*values: object) -> bool | None:
    for value in values:
        parsed = _claim_optional_bool(value)
        if parsed is not None:
            return parsed
    return None


def _prefer_userinfo_claim(current: bool | None, *values: object) -> bool | None:
    parsed = _first_claim_bool(*values)
    return current if parsed is None else parsed


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    issuer: str
    subject: str
    audience: str | None = None
    email: str | None = None
    phone: str | None = None
    email_verified: bool | None = None
    phone_verified: bool | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    username: str | None = None
    owner: str | None = None
    issued_at: int | None = None
    is_admin: bool = False
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()

    def merge_userinfo(self, userinfo: dict[str, object]) -> "AuthenticatedPrincipal":
        return AuthenticatedPrincipal(
            issuer=self.issuer,
            subject=self.subject,
            audience=self.audience,
            email=self.email or _string_claim(userinfo.get("email")),
            phone=self.phone or _string_claim(userinfo.get("phone")),
            email_verified=_prefer_userinfo_claim(
                self.email_verified,
                userinfo.get("emailVerified"),
                userinfo.get("email_verified"),
            ),
            phone_verified=_prefer_userinfo_claim(
                self.phone_verified,
                userinfo.get("phoneVerified"),
                userinfo.get("phone_verified"),
            ),
            display_name=self.display_name
            or _string_claim(userinfo.get("name"))
            or _string_claim(userinfo.get("displayName")),
            avatar_url=self.avatar_url
            or _string_claim(userinfo.get("picture"))
            or _string_claim(userinfo.get("avatar"))
            or _string_claim(userinfo.get("avatarUrl")),
            username=self.username or _string_claim(userinfo.get("name")),
            owner=self.owner or _string_claim(userinfo.get("owner")),
            issued_at=self.issued_at,
            is_admin=self.is_admin
            or _claim_bool(userinfo.get("isAdmin"))
            or _claim_bool(userinfo.get("is_admin")),
            roles=tuple(dict.fromkeys((*self.roles, *_claim_names(userinfo.get("roles"))))),
            permissions=tuple(
                dict.fromkeys((*self.permissions, *_claim_names(userinfo.get("permissions"))))
            ),
        )


def _string_claim(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class CasdoorTokenVerifier:
    """Casdoor OIDC Access Token 验证器。"""

    _jwks_client: PyJWKClient | None = None
    _jwks_fetched_at: float = 0
    _jwks_ttl: float = 600.0

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def required_organization(self) -> str | None:
        organization = (self._settings.casdoor_organization or "").strip()
        return organization or None

    def ensure_organization(self, principal: AuthenticatedPrincipal) -> None:
        organization = self.required_organization
        if not organization or principal.owner == organization:
            return
        raise ApplicationError(
            code="IDENTITY_ORGANIZATION_MISMATCH",
            message="请使用 Moment One 所属组织账号重新登录。",
            status_code=401,
            details={"expectedOrganization": organization},
        )

    def ensure_account_scope(self, account: dict[str, object]) -> None:
        """确保当前 Casdoor 用户属于配置的组织，并由配置的应用创建。"""
        organization = (self._settings.casdoor_organization or "").strip()
        application = (self._settings.casdoor_application or "").strip()
        actual_organization = str(account.get("owner") or "").strip()
        actual_application = str(account.get("signupApplication") or "").strip()
        if actual_organization == organization and actual_application == application:
            return
        raise ApplicationError(
            code="IDENTITY_ORGANIZATION_MISMATCH",
            message="请使用 Moment One 所属组织和应用的账号重新登录。",
            status_code=401,
            details={
                "expectedOrganization": organization,
                "expectedApplication": application,
            },
        )

    def _ensure_jwks_client(self) -> PyJWKClient:
        now = time.monotonic()
        if self._jwks_client is None or now - self._jwks_fetched_at > self._jwks_ttl:
            jwks_url = (
                str(self._settings.casdoor_jwks_url) if self._settings.casdoor_jwks_url else None
            )
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
        try:
            jwks = self._ensure_jwks_client()
            signing_key = jwks.get_signing_key_from_jwt(access_token)
            issuer = str(self._settings.casdoor_issuer).rstrip("/")
            audience = self._settings.casdoor_audience
            unverified = jwt.decode(access_token, options={"verify_signature": False})
            has_aud = "aud" in unverified
            payload: dict[str, object] = jwt.decode(
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
                email=_string_claim(payload.get("email")),
                phone=_string_claim(payload.get("phone")),
                email_verified=_first_claim_bool(
                    payload.get("email_verified"), payload.get("emailVerified")
                ),
                phone_verified=_first_claim_bool(
                    payload.get("phone_verified"), payload.get("phoneVerified")
                ),
                display_name=_string_claim(payload.get("name"))
                or _string_claim(payload.get("preferred_username")),
                avatar_url=_string_claim(payload.get("picture"))
                or _string_claim(payload.get("avatar"))
                or _string_claim(payload.get("avatarUrl")),
                issued_at=(lambda value: value if isinstance(value, int) else None)(
                    payload.get("iat")
                ),
                is_admin=_claim_bool(payload.get("isAdmin"))
                or _claim_bool(payload.get("is_admin")),
                roles=_claim_names(payload.get("roles")),
                permissions=_claim_names(payload.get("permissions")),
            )
        except jwt.ExpiredSignatureError:
            raise ApplicationError(
                code="TOKEN_INVALID", message="Access Token 已过期，请重新登录。", status_code=401
            ) from None
        except jwt.InvalidAudienceError:
            raise ApplicationError(
                code="TOKEN_INVALID", message="Access Token 受众不匹配。", status_code=401
            ) from None
        except jwt.InvalidIssuerError:
            raise ApplicationError(
                code="TOKEN_INVALID", message="Access Token 签发方不匹配。", status_code=401
            ) from None
        except ApplicationError:
            raise
        except Exception:
            raise ApplicationError(
                code="TOKEN_INVALID", message="Access Token 验证失败。", status_code=401
            ) from None

    async def fetch_userinfo(self, access_token: str) -> dict[str, object]:
        userinfo_url = f"{str(self._settings.casdoor_issuer).rstrip('/')}/api/userinfo"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}

    async def fetch_account(self, access_token: str) -> dict[str, object]:
        account_url = f"{str(self._settings.casdoor_issuer).rstrip('/')}/api/get-account"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                account_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                return {}
            data = payload.get("data")
            return data if isinstance(data, dict) else {}


__all__ = [
    "AuthenticatedPrincipal",
    "CasdoorTokenVerifier",
    "_claim_bool",
    "_claim_optional_bool",
    "_first_claim_bool",
    "_prefer_userinfo_claim",
    "_claim_names",
]
