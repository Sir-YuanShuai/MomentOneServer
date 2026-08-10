import base64
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.core.errors import ApplicationError

PROVIDER_TYPES = {
    "github": "GitHub",
    "gitee": "Gitee",
    "google": "Google",
    "qq": "QQ",
}
PROVIDER_AUTH = {
    "github": {
        "endpoint": "https://github.com/login/oauth/authorize",
        "scope": "user:email read:user",
    },
    "gitee": {
        "endpoint": "https://gitee.com/oauth/authorize",
        "scope": "user_info emails",
    },
    "google": {
        "endpoint": "https://accounts.google.com/signin/oauth",
        "scope": "profile email",
    },
    "qq": {
        "endpoint": "https://graph.qq.com/oauth2.0/authorize",
        "scope": "get_user_info",
    },
}


def generate_pkce_verifier() -> str:
    return base64.urlsafe_b64encode(__import__("secrets").token_bytes(48)).rstrip(b"=").decode()


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class CasdoorManagementClient:
    """只在 Server 内使用的 Casdoor 用户管理与账号关联适配器。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return bool(self._issuer and self._client_id and self._client_secret)

    @property
    def _issuer(self) -> str:
        return (
            str(self._settings.casdoor_issuer).rstrip("/") if self._settings.casdoor_issuer else ""
        )

    @property
    def _client_id(self) -> str:
        return (
            self._settings.casdoor_management_client_id
            or self._settings.casdoor_mcp_client_id
            or ""
        )

    @property
    def _client_secret(self) -> str:
        return (
            self._settings.casdoor_management_client_secret
            or self._settings.casdoor_mcp_client_secret
            or ""
        )

    def _require(self, *, application: bool = False) -> None:
        if not self.configured:
            raise ApplicationError(
                code="IDENTITY_SYNC_NOT_CONFIGURED",
                message="账号资料同步服务尚未配置。",
                status_code=503,
            )
        if application and not (
            self._settings.casdoor_organization and self._settings.casdoor_application
        ):
            raise ApplicationError(
                code="IDENTITY_VERIFICATION_NOT_CONFIGURED",
                message="邮箱和手机号验证服务尚未配置。",
                status_code=503,
            )

    def _credentials(self) -> dict[str, str]:
        return {"clientId": self._client_id, "clientSecret": self._client_secret}

    @staticmethod
    def _data(payload: Any) -> Any:
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            message = payload.get("msg") if isinstance(payload, dict) else None
            raise ApplicationError(
                code="IDENTITY_UPSTREAM_ERROR",
                message=str(message or "Casdoor 返回了无法识别的响应。"),
                status_code=502,
            )
        return payload.get("data")

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int | bool] | None = None,
        data: Mapping[str, str | int | bool] | None = None,
        json: dict[str, object] | None = None,
        access_token: str | None = None,
    ) -> Any:
        self._require()
        query: dict[str, str | int | bool] = {**(params or {})}
        headers: dict[str, str] = {}
        if access_token:
            # 用户自助操作：以用户自己的 Casdoor access token 鉴权，
            # 让 Casdoor 强制"只能操作自己的资料"，而不是用超管应用凭据。
            headers["Authorization"] = f"Bearer {access_token}"
        else:
            # 系统级操作（发码/验码/OAuth 换码）：使用应用 clientId/clientSecret。
            query.update(self._credentials())
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.request(
                    method,
                    f"{self._issuer}{path}",
                    params=query,
                    data=data,
                    json=json,
                    headers=headers,
                )
            response.raise_for_status()
            return self._data(response.json())
        except ApplicationError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ApplicationError(
                code="IDENTITY_UPSTREAM_ERROR",
                message="Casdoor 暂时不可用，请稍后重试。",
                status_code=502,
            ) from exc

    async def get_user(
        self, casdoor_user_id: str, *, access_token: str | None = None
    ) -> dict[str, object]:
        data = await self._json_request(
            "GET",
            "/api/get-user",
            params={"userId": casdoor_user_id},
            access_token=access_token,
        )
        if not isinstance(data, dict):
            raise ApplicationError(
                code="IDENTITY_NOT_FOUND", message="Casdoor 用户不存在。", status_code=404
            )
        return data

    async def get_application(self) -> dict[str, object]:
        app_id = (
            f"{self._settings.casdoor_organization}/{self._settings.casdoor_application}"
            if self._settings.casdoor_organization and self._settings.casdoor_application
            else "admin/MomentOne"
        )
        data = await self._json_request("GET", "/api/get-application", params={"id": app_id})
        if not isinstance(data, dict):
            raise ApplicationError(
                code="IDENTITY_APPLICATION_NOT_FOUND",
                message="身份应用不存在。",
                status_code=502,
            )
        return data

    async def provider_link_url(
        self,
        *,
        provider: str,
        return_uri: str,
    ) -> str:
        provider_type = PROVIDER_TYPES.get(provider)
        provider_auth = PROVIDER_AUTH.get(provider)
        if provider_type is None or provider_auth is None:
            raise ApplicationError(
                code="IDENTITY_LINK_PROVIDER_UNSUPPORTED",
                message="暂不支持该登录方式。",
                status_code=400,
            )

        application = await self.get_application()
        provider_item = self._application_provider(application, provider_type)
        provider_obj = provider_item.get("provider")
        if not isinstance(provider_obj, dict):
            raise ApplicationError(
                code="IDENTITY_LINK_PROVIDER_NOT_CONFIGURED",
                message="该登录方式尚未配置。",
                status_code=503,
            )
        client_id = str(provider_obj.get("clientId") or "")
        provider_name = str(provider_obj.get("name") or "")
        if not client_id or not provider_name:
            raise ApplicationError(
                code="IDENTITY_LINK_PROVIDER_NOT_CONFIGURED",
                message="该登录方式尚未配置完整。",
                status_code=503,
            )

        application_name = str(application.get("name") or "MomentOne")
        if application.get("isShared"):
            application_name = f"{application_name}-org-{application.get('organization')}"
        redirect_origin = str(application.get("forcedRedirectOrigin") or self._issuer).rstrip("/")
        redirect_uri = f"{redirect_origin}/callback"
        scope = str(provider_obj.get("scopes") or provider_auth["scope"])
        scope = scope.replace("%20", " ").replace("+", " ")
        state_query = urlencode(
            {
                "application": application_name,
                "provider": provider_name,
                "method": "link",
                "from": return_uri,
            }
        )
        state = base64.b64encode(f"&{state_query}".encode()).decode()
        return f"{provider_auth['endpoint']}?{
            urlencode(
                {
                    'client_id': client_id,
                    'redirect_uri': redirect_uri,
                    'scope': scope,
                    'response_type': 'code',
                    'state': state,
                }
            )
        }"

    @staticmethod
    def _application_provider(
        application: dict[str, object], provider_type: str
    ) -> dict[str, object]:
        providers = application.get("providers")
        if not isinstance(providers, list):
            providers = []
        for item in providers:
            if not isinstance(item, dict):
                continue
            provider_obj = item.get("provider")
            if isinstance(provider_obj, dict) and provider_obj.get("type") == provider_type:
                return item
        raise ApplicationError(
            code="IDENTITY_LINK_PROVIDER_NOT_CONFIGURED",
            message="该登录方式尚未配置。",
            status_code=503,
        )

    async def find_user(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        access_token: str | None = None,
    ) -> dict | None:
        params: dict[str, str] = {}
        if email:
            params["email"] = email
        elif phone:
            params["phone"] = phone
        else:
            return None
        try:
            data = await self._json_request(
                "GET", "/api/get-user", params=params, access_token=access_token
            )
        except ApplicationError as exc:
            missing_markers = ("doesn't exist", "does not exist", "不存在", "not found")
            if exc.code in {"IDENTITY_NOT_FOUND", "IDENTITY_UPSTREAM_ERROR"} and any(
                marker in exc.message.lower() for marker in missing_markers
            ):
                return None
            raise
        return data if isinstance(data, dict) else None

    @staticmethod
    def _require_user_token(access_token: str | None) -> None:
        # 用户自助修改必须携带用户自己的 Casdoor token。
        # 绝不能回退到应用 clientId/clientSecret（超管身份），否则用户能借超管凭据改他人资料。
        if not access_token:
            raise ApplicationError(
                code="WEB_SESSION_REQUIRED",
                message="该操作需要 Casdoor 用户会话。",
                status_code=401,
            )

    async def update_user(
        self, user: dict[str, object], *, access_token: str | None = None
    ) -> dict[str, object]:
        self._require_user_token(access_token)
        owner = str(user.get("owner") or "")
        name = str(user.get("name") or "")
        if not owner or not name:
            raise ApplicationError(
                code="IDENTITY_UPSTREAM_ERROR",
                message="Casdoor 用户缺少 owner/name。",
                status_code=502,
            )
        await self._json_request(
            "POST",
            "/api/update-user",
            params={"id": f"{owner}/{name}"},
            json=user,
            access_token=access_token,
        )
        return user

    async def unlink_provider(
        self,
        casdoor_user_id: str,
        *,
        provider: str,
        access_token: str | None = None,
    ) -> None:
        self._require_user_token(access_token)
        provider_type = PROVIDER_TYPES.get(provider)
        if provider_type is None:
            raise ApplicationError(
                code="IDENTITY_LINK_PROVIDER_UNSUPPORTED",
                message="暂不支持该登录方式。",
                status_code=400,
            )
        user = await self.get_user(casdoor_user_id)
        raw_props = user.get("properties")
        props = raw_props if isinstance(raw_props, dict) else {}
        user["properties"] = {
            key: value
            for key, value in props.items()
            if not key.startswith(f"oauth_{provider_type}_")
        }
        user[provider] = ""
        await self.update_user(user, access_token=access_token)

    async def update_profile(
        self,
        casdoor_user_id: str,
        *,
        display_name: str | None = None,
        avatar_url: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        email_verified: bool | None = None,
        phone_verified: bool | None = None,
        access_token: str | None = None,
    ) -> dict[str, object]:
        self._require_user_token(access_token)
        user = await self.get_user(casdoor_user_id, access_token=access_token)
        if display_name is not None:
            user["displayName"] = display_name
        if avatar_url is not None:
            user["avatar"] = avatar_url
        if email is not None:
            user["email"] = email
        if phone is not None:
            user["phone"] = phone
        if email_verified is not None:
            user["emailVerified"] = email_verified
        if phone_verified is not None:
            user["phoneVerified"] = phone_verified
        return await self.update_user(user, access_token=access_token)

    async def upload_avatar(
        self,
        casdoor_user_id: str,
        *,
        filename: str,
        content_type: str,
        content: bytes,
        access_token: str | None = None,
    ) -> str:
        self._require_user_token(access_token)
        user = await self.get_user(casdoor_user_id, access_token=access_token)
        owner = str(user.get("owner") or self._settings.casdoor_organization or "")
        name = str(user.get("name") or "")
        application = str(self._settings.casdoor_application or user.get("signupApplication") or "")
        if not owner or not name or not application:
            raise ApplicationError(
                code="IDENTITY_VERIFICATION_NOT_CONFIGURED",
                message="Casdoor 用户缺少组织或应用信息。",
                status_code=503,
            )
        suffix = Path(filename).suffix.lower() or ".jpg"
        params = {
            "owner": owner,
            "user": name,
            "application": application,
            "tag": "avatar",
            "parent": "",
            "fullFilePath": f"avatars/{name}{suffix}",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self._issuer}/api/upload-resource",
                    params=params,
                    files={"file": (filename, content, content_type)},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            response.raise_for_status()
            payload = response.json()
            data = self._data(payload)
        except ApplicationError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ApplicationError(
                code="AVATAR_UPLOAD_FAILED",
                message="头像上传失败，请稍后重试。",
                status_code=502,
            ) from exc
        if isinstance(data, str):
            return data
        if isinstance(data, list) and data and isinstance(data[0], str):
            return data[0]
        if isinstance(data, dict):
            for key in ("url", "fileUrl", "Url"):
                if isinstance(data.get(key), str):
                    return str(data[key])
        refreshed = await self.get_user(casdoor_user_id)
        avatar = refreshed.get("avatar")
        if isinstance(avatar, str) and avatar:
            return avatar
        raise ApplicationError(
            code="AVATAR_UPLOAD_FAILED", message="Casdoor 未返回头像地址。", status_code=502
        )

    async def set_password(
        self,
        casdoor_user_id: str,
        *,
        old_password: str,
        new_password: str,
        access_token: str | None = None,
    ) -> None:
        self._require_user_token(access_token)
        user = await self.get_user(casdoor_user_id, access_token=access_token)
        data = {
            "userOwner": str(user.get("owner") or ""),
            "userName": str(user.get("name") or ""),
            "oldPassword": old_password,
            "newPassword": new_password,
        }
        await self._json_request("POST", "/api/set-password", data=data, access_token=access_token)

    async def send_contact_code(
        self,
        *,
        kind: str,
        destination: str,
        country_code: str | None,
        application_id: str,
    ) -> None:
        self._require()
        form = {
            "dest": destination,
            "type": kind,
            "countryCode": country_code or "",
            "applicationId": application_id,
            "method": "signup",
            "captchaType": "none",
        }
        await self._json_request("POST", "/api/send-verification-code", data=form)

    async def verify_contact_code(
        self,
        *,
        kind: str,
        destination: str,
        country_code: str | None,
        code: str,
        organization: str,
    ) -> None:
        self._require()
        await self._json_request(
            "POST",
            "/api/verify-code",
            json={
                "organization": organization,
                "username": destination,
                "countryCode": country_code or "",
                "code": code,
                "type": kind,
            },
        )

    async def get_sessions(
        self, owner: str, *, access_token: str | None = None
    ) -> list[dict[str, object]]:
        data = await self._json_request(
            "GET", "/api/get-sessions", params={"owner": owner}, access_token=access_token
        )
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    async def delete_session(
        self, session: dict[str, object], *, access_token: str | None = None
    ) -> None:
        self._require_user_token(access_token)
        owner = str(session.get("owner") or "")
        name = str(session.get("name") or "")
        if not owner or not name:
            raise ApplicationError(
                code="SESSION_NOT_FOUND", message="登录会话不存在。", status_code=404
            )
        await self._json_request(
            "POST",
            "/api/delete-session",
            params={"id": f"{owner}/{name}"},
            json=session,
            access_token=access_token,
        )

    def authorize_url(
        self,
        *,
        state: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> str:
        self._require()
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self._settings.casdoor_mcp_scope,
            "state": state,
            "code_challenge": pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
            # 必须强制走登录页：否则已登录用户会直接 auto-grant，不会真正授权 Provider，
            # 绑定就变成无效操作。在弹窗里显示登录页选 Provider 即可完成绑定。
            "prompt": "login",
            "max_age": "0",
        }
        return f"{self._issuer}/login/oauth/authorize?{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> dict[str, object]:
        self._require()
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code_verifier": code_verifier,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{self._issuer}/api/login/oauth/access_token", data=payload
                )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ApplicationError(
                code="IDENTITY_LINK_UPSTREAM_ERROR",
                message="登录方式验证失败，请重新发起绑定。",
                status_code=502,
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
            raise ApplicationError(
                code="IDENTITY_LINK_UPSTREAM_ERROR",
                message="Casdoor 未返回有效 Access Token。",
                status_code=502,
            )
        return data
