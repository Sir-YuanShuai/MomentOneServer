from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Response, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth_context
from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.session import get_db_session
from app.infrastructure.identity.casdoor import CasdoorTokenVerifier
from app.infrastructure.identity.casdoor_management import CasdoorManagementClient
from app.infrastructure.storage.object_storage import (
    ObjectStorage,
    ObjectStorageNotConfigured,
    get_object_storage,
)
from app.modules.account_deletion.service import AccountDeletionService
from app.modules.accounts.providers import login_providers_from_casdoor
from app.modules.accounts.service import AccountCenterService
from app.modules.admin.account import AccountRepository

router = APIRouter(prefix="/v1/account", tags=["account"])


def _account_repo(session: AsyncSession = Depends(get_db_session)) -> AccountRepository:
    return AccountRepository(session)


def _account_center_service(
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AccountCenterService:
    return AccountCenterService(session, settings)


class UpdateProfileRequest(BaseModel):
    # displayName 已由浏览器直连 Casdoor 修改，此处仅保留本地偏好字段。
    locale: str = Field(default="zh-CN", min_length=2, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)
    expectedRevision: int = Field(ge=1)


class ChangePasswordRequest(BaseModel):
    oldPassword: str = Field(min_length=1, max_length=256)
    newPassword: str = Field(min_length=8, max_length=256)


class ContactChallengeRequest(BaseModel):
    kind: Literal["email", "phone"]
    destination: str = Field(min_length=3, max_length=255)
    countryCode: str | None = Field(default=None, max_length=8)
    expectedRevision: int = Field(ge=1)


class ContactConfirmRequest(BaseModel):
    code: str = Field(min_length=4, max_length=12)


class LinkSessionRequest(BaseModel):
    provider: str | None = Field(default=None, max_length=64)
    returnUri: str | None = Field(default=None, max_length=500)


class IdentityUnlinkConfirmRequest(BaseModel):
    confirmationId: UUID
    confirmationPhrase: str = Field(min_length=1, max_length=32)


class DeleteAccountConfirmRequest(BaseModel):
    confirmationId: UUID
    confirmationPhrase: str = Field(min_length=1, max_length=32)


def _require_idempotency(value: str | None) -> None:
    if not value:
        raise ApplicationError(
            code="INVALID_REQUEST",
            message="写操作必须提供 Idempotency-Key。",
            status_code=400,
        )


def _login_providers(casdoor_user: dict[str, object]) -> list[dict[str, str]]:
    """从 Casdoor 用户对象推导已绑定的第三方登录。

    Casdoor 把绑定的 Provider 存在用户对象的 properties.oauth_<Provider>_* 里
    （get-account 对端不一定返回 properties），因此由服务端用应用凭据读取并暴露。
    """
    return login_providers_from_casdoor(casdoor_user)


async def _read_casdoor_user(
    casdoor_user_id: str,
    *,
    auth: AuthContext,
    client: CasdoorManagementClient,
) -> dict[str, object]:
    # 账号只读优先使用当前用户的 Casdoor token，避免生产环境必须额外配置
    # 管理凭据；应用凭据作为只读兜底，兼容 token 权限不足的租户配置。
    if auth.raw_access_token:
        try:
            return await client.get_user(casdoor_user_id, access_token=auth.raw_access_token)
        except ApplicationError:
            pass
    return await client.get_user(casdoor_user_id)


async def _attach_login_providers(
    account: dict[str, object],
    *,
    auth: AuthContext,
    session: AsyncSession,
    settings: Settings,
) -> dict[str, object]:
    """附加 Casdoor 登录方式状态，并区分“未绑定”和“暂时读不到”。"""
    account["loginProviders"] = []
    account["loginProvidersStatus"] = "not_available"
    if auth.method != "casdoor":
        return account
    try:
        local_user = await UserRepository(session).get(auth.user_id)
        if local_user is None or not local_user.casdoor_user_id:
            account["loginProvidersStatus"] = "unavailable"
            return account
        casdoor_user = await _read_casdoor_user(
            local_user.casdoor_user_id,
            auth=auth,
            client=CasdoorManagementClient(settings),
        )
        account["loginProviders"] = _login_providers(casdoor_user)
        account["loginProvidersStatus"] = "available"
    except Exception:
        # Casdoor 暂时不可用时保持账户主体可用，但显式标记绑定状态不可读。
        account["loginProvidersStatus"] = "unavailable"
    return account


def _account_storage(settings: Settings = Depends(get_settings)) -> ObjectStorage | None:
    try:
        return get_object_storage(settings)
    except ObjectStorageNotConfigured:
        return None


def _deletion_service(
    session: AsyncSession = Depends(get_db_session),
    storage: ObjectStorage | None = Depends(_account_storage),
) -> AccountDeletionService:
    return AccountDeletionService(session, storage)


@router.get("")
async def get_account(
    response: Response,
    auth: AuthContext = Depends(get_auth_context),
    repo: AccountRepository = Depends(_account_repo),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    response.headers["Cache-Control"] = "private, no-store"
    if auth.method == "casdoor" and authorization:
        token = authorization.removeprefix("Bearer ").strip()
        verifier = CasdoorTokenVerifier(settings)
        try:
            principal = verifier.verify(token).merge_userinfo(await verifier.fetch_userinfo(token))
            user = await UserRepository(session).get(auth.user_id)
            if user is not None:
                # 浏览器直连 Casdoor 修改资料后，这里每次读取都覆盖同步本地身份字段，
                # 保证本地业务展示始终与身份系统一致（语言/时区等本地偏好不受影响）。
                await UserRepository(session).sync_profile(user, principal, fill_missing_only=False)
        except Exception:
            # Casdoor 短暂不可用时，账号中心继续使用本地同步成功的资料。
            pass
    account = await repo.account(auth.user_id)
    if account is None:
        raise ApplicationError(code="USER_NOT_FOUND", message="用户不存在。", status_code=404)
    account["accessSource"] = {
        "method": auth.method,
        "clientId": auth.client_id,
        "deviceId": auth.device_id,
    }
    # 该用户绑定的第三方登录：get-account 不返回 properties，由服务端用应用凭据读取。
    return await _attach_login_providers(account, auth=auth, session=session, settings=settings)


@router.patch("/profile")
async def update_profile(
    body: UpdateProfileRequest,
    auth: AuthContext = Depends(get_auth_context),
    service: AccountCenterService = Depends(_account_center_service),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    _require_idempotency(idempotency_key)
    if auth.method != "casdoor":
        raise ApplicationError(
            code="WEB_SESSION_REQUIRED", message="请使用 Web 登录修改账号资料。", status_code=403
        )
    account = await service.update_profile(
        auth.user_id,
        locale=body.locale,
        timezone=body.timezone,
        expected_revision=body.expectedRevision,
    )
    return await _attach_login_providers(account, auth=auth, session=session, settings=settings)


@router.post("/sync")
async def sync_account_profile(
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    """浏览器在 Casdoor 直接修改资料后，把本地 users 表从 Casdoor userinfo 覆盖同步。"""
    _require_idempotency(idempotency_key)
    if auth.method != "casdoor":
        raise ApplicationError(
            code="WEB_SESSION_REQUIRED", message="请使用 Web 登录同步账号资料。", status_code=403
        )
    if not authorization:
        raise ApplicationError(code="AUTH_REQUIRED", message="请先登录。", status_code=401)
    token = authorization.removeprefix("Bearer ").strip()
    verifier = CasdoorTokenVerifier(settings)
    principal = verifier.verify(token).merge_userinfo(await verifier.fetch_userinfo(token))
    user = await UserRepository(session).get(auth.user_id)
    if user is None:
        raise ApplicationError(code="USER_NOT_FOUND", message="用户不存在。", status_code=404)
    await UserRepository(session).sync_profile(user, principal, fill_missing_only=False)
    account = await AccountRepository(session).account(auth.user_id)
    if account is None:
        raise ApplicationError(code="USER_NOT_FOUND", message="用户不存在。", status_code=404)
    return await _attach_login_providers(account, auth=auth, session=session, settings=settings)


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    expectedRevision: int = Form(..., ge=1),
    auth: AuthContext = Depends(get_auth_context),
    service: AccountCenterService = Depends(_account_center_service),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    _require_idempotency(idempotency_key)
    if auth.method != "casdoor":
        raise ApplicationError(
            code="WEB_SESSION_REQUIRED", message="请使用 Web 登录修改头像。", status_code=403
        )
    account = await service.upload_avatar(
        auth.user_id,
        filename=file.filename or "avatar.jpg",
        content_type=file.content_type or "application/octet-stream",
        content=await file.read(),
        expected_revision=expectedRevision,
        access_token=auth.raw_access_token,
    )
    return await _attach_login_providers(account, auth=auth, session=session, settings=settings)


@router.post("/password")
async def change_password(
    body: ChangePasswordRequest,
    auth: AuthContext = Depends(get_auth_context),
    service: AccountCenterService = Depends(_account_center_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, bool]:
    _require_idempotency(idempotency_key)
    if auth.method != "casdoor":
        raise ApplicationError(
            code="REAUTHENTICATION_REQUIRED",
            message="修改密码必须使用用户自己的身份会话。",
            status_code=401,
        )
    await service.change_password(
        auth.user_id,
        old_password=body.oldPassword,
        new_password=body.newPassword,
        issued_at=auth.issued_at,
        access_token=auth.raw_access_token,
    )
    return {"changed": True}


@router.get("/identities")
async def list_identities(
    auth: AuthContext = Depends(get_auth_context),
    service: AccountCenterService = Depends(_account_center_service),
) -> dict[str, object]:
    return await service.identities(auth.user_id, access_token=auth.raw_access_token)


@router.delete("/sessions/{session_name}", status_code=204)
async def revoke_session(
    session_name: str,
    application: str,
    auth: AuthContext = Depends(get_auth_context),
    service: AccountCenterService = Depends(_account_center_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    _require_idempotency(idempotency_key)
    await service.revoke_session(
        auth.user_id,
        session_name=session_name,
        application=application,
        access_token=auth.raw_access_token,
    )
    return Response(status_code=204)


@router.delete("/login-providers/{provider}", status_code=204)
async def unlink_login_provider(
    provider: str,
    auth: AuthContext = Depends(get_auth_context),
    service: AccountCenterService = Depends(_account_center_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    _require_idempotency(idempotency_key)
    if auth.method != "casdoor":
        raise ApplicationError(
            code="WEB_SESSION_REQUIRED", message="请使用 Web 登录解除登录方式。", status_code=403
        )
    await service.unlink_login_provider(
        auth.user_id,
        provider=provider,
        access_token=auth.raw_access_token,
    )
    return Response(status_code=204)


@router.post("/contact-challenges", status_code=201)
async def start_contact_challenge(
    body: ContactChallengeRequest,
    auth: AuthContext = Depends(get_auth_context),
    service: AccountCenterService = Depends(_account_center_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    _require_idempotency(idempotency_key)
    if auth.method != "casdoor":
        raise ApplicationError(
            code="WEB_SESSION_REQUIRED", message="请使用 Web 登录绑定联系方式。", status_code=403
        )
    return await service.start_contact_challenge(
        auth.user_id,
        kind=body.kind,
        destination=body.destination,
        country_code=body.countryCode,
        expected_revision=body.expectedRevision,
        access_token=auth.raw_access_token,
    )


@router.post("/contact-challenges/{challenge_id}/confirm")
async def confirm_contact_challenge(
    challenge_id: UUID,
    body: ContactConfirmRequest,
    auth: AuthContext = Depends(get_auth_context),
    service: AccountCenterService = Depends(_account_center_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    _require_idempotency(idempotency_key)
    return await service.confirm_contact_challenge(
        auth.user_id,
        challenge_id=challenge_id,
        code=body.code,
        access_token=auth.raw_access_token,
    )


@router.delete("/contact-challenges/{challenge_id}", status_code=204)
async def cancel_contact_challenge(
    challenge_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    service: AccountCenterService = Depends(_account_center_service),
) -> Response:
    await service.cancel_contact_challenge(
        auth.user_id, challenge_id, access_token=auth.raw_access_token
    )
    return Response(status_code=204)


@router.post("/link-sessions", status_code=201)
async def start_link_session(
    body: LinkSessionRequest,
    auth: AuthContext = Depends(get_auth_context),
    service: AccountCenterService = Depends(_account_center_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    _require_idempotency(idempotency_key)
    if auth.method != "casdoor":
        raise ApplicationError(
            code="WEB_SESSION_REQUIRED", message="请使用 Web 登录绑定登录方式。", status_code=403
        )
    return await service.start_link_session(
        auth.user_id, provider=body.provider, return_uri=body.returnUri
    )


@router.get("/link-callback", include_in_schema=False)
async def link_callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
    service: AccountCenterService = Depends(_account_center_service),
) -> RedirectResponse:
    return RedirectResponse(
        await service.complete_link_session(state=state, code=code, error=error), status_code=302
    )


@router.post("/identities/{identity_id}/unlink-preview")
async def identity_unlink_preview(
    identity_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    service: AccountCenterService = Depends(_account_center_service),
) -> dict[str, object]:
    return await service.unlink_preview(auth.user_id, identity_id)


@router.post("/identities/{identity_id}/unlink-confirm")
async def identity_unlink_confirm(
    identity_id: UUID,
    body: IdentityUnlinkConfirmRequest,
    auth: AuthContext = Depends(get_auth_context),
    service: AccountCenterService = Depends(_account_center_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    del identity_id
    if not idempotency_key:
        raise ApplicationError(
            code="INVALID_REQUEST",
            message="解除登录方式必须提供 Idempotency-Key。",
            status_code=400,
        )
    return await service.unlink_confirm(
        auth.user_id,
        confirmation_id=body.confirmationId,
        phrase=body.confirmationPhrase,
        issued_at=auth.issued_at,
    )


@router.get("/merge-preview")
async def merge_preview(
    linkSessionId: UUID,
    auth: AuthContext = Depends(get_auth_context),
    service: AccountCenterService = Depends(_account_center_service),
) -> dict[str, object]:
    return await service.merge_preview(auth.user_id, linkSessionId)


@router.post("/delete-preview")
async def delete_account_preview(
    auth: AuthContext = Depends(get_auth_context),
    service: AccountDeletionService = Depends(_deletion_service),
) -> dict[str, object]:
    if auth.method != "casdoor":
        raise ApplicationError(
            code="REAUTHENTICATION_REQUIRED",
            message="账号注销必须通过 Casdoor 用户会话发起。",
            status_code=401,
        )
    return await service.preview(auth.user_id)


@router.post("/delete-confirm")
async def delete_account_confirm(
    body: DeleteAccountConfirmRequest,
    auth: AuthContext = Depends(get_auth_context),
    service: AccountDeletionService = Depends(_deletion_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    if not idempotency_key:
        raise ApplicationError(
            code="INVALID_REQUEST",
            message="账号注销必须提供 Idempotency-Key。",
            status_code=400,
        )
    if auth.method != "casdoor":
        raise ApplicationError(
            code="REAUTHENTICATION_REQUIRED",
            message="账号注销必须重新验证 Casdoor 登录密码。",
            status_code=401,
        )
    return await service.confirm(
        auth.user_id,
        confirmation_id=body.confirmationId,
        confirmation_phrase=body.confirmationPhrase,
        issued_at=auth.issued_at,
    )


__all__ = ["router"]
