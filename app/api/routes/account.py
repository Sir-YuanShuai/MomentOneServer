from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth_context
from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.session import get_db_session
from app.infrastructure.identity.casdoor import CasdoorTokenVerifier
from app.infrastructure.storage.object_storage import (
    ObjectStorage,
    ObjectStorageNotConfigured,
    get_object_storage,
)
from app.modules.account_deletion.service import AccountDeletionService
from app.modules.admin.account import AccountRepository

router = APIRouter(prefix="/v1/account", tags=["account"])


def _account_repo(session: AsyncSession = Depends(get_db_session)) -> AccountRepository:
    return AccountRepository(session)


class DeleteAccountConfirmRequest(BaseModel):
    confirmationId: UUID
    confirmationPhrase: str = Field(min_length=1, max_length=32)


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
    auth: AuthContext = Depends(get_auth_context),
    repo: AccountRepository = Depends(_account_repo),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    avatar_url: str | None = None
    if auth.method == "casdoor" and authorization:
        token = authorization.removeprefix("Bearer ").strip()
        verifier = CasdoorTokenVerifier(settings)
        try:
            principal = verifier.verify(token).merge_userinfo(await verifier.fetch_userinfo(token))
            user = await UserRepository(session).get(auth.user_id)
            if user is not None:
                await UserRepository(session).sync_profile(user, principal)
            avatar_url = principal.avatar_url
        except Exception:
            # 账号页仍可使用已同步的本地资料；Casdoor 短暂不可用不阻断额度查询。
            avatar_url = None
    account = await repo.account(auth.user_id, avatar_url=avatar_url)
    if account is None:
        raise ApplicationError(code="USER_NOT_FOUND", message="用户不存在。", status_code=404)
    account["accessSource"] = {
        "method": auth.method,
        "clientId": auth.client_id,
        "deviceId": auth.device_id,
    }
    return account


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
