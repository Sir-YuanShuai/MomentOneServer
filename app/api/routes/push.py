from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth_context
from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.infrastructure.database.models.push_subscription import PushSubscription
from app.infrastructure.database.repositories.push_subscription_repository import (
    PushSubscriptionRepository,
)
from app.infrastructure.database.session import get_db_session
from app.modules.notifications.push import (
    PushSecretCipher,
    PushSecrets,
    WebPushSender,
    endpoint_hash,
    validate_push_endpoint,
)

router = APIRouter(prefix="/v1/push", tags=["push-notifications"])


async def _get_web_user_id(context: AuthContext = Depends(get_auth_context)) -> UUID:
    if context.method != "casdoor":
        raise ApplicationError(
            code="WEB_SESSION_REQUIRED",
            message="通知终端只能由已登录的 Web 应用管理。",
            status_code=403,
        )
    return context.user_id


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=20, max_length=512)
    auth: str = Field(min_length=8, max_length=256)


class RegisterPushSubscriptionRequest(BaseModel):
    endpoint: str = Field(min_length=16, max_length=4096)
    expirationTime: datetime | None = None
    keys: PushKeys
    contentEncoding: str = Field(default="aes128gcm", max_length=32)
    platform: str | None = Field(default=None, max_length=32)
    deviceLabel: str | None = Field(default=None, max_length=120)


def _repo(session: AsyncSession = Depends(get_db_session)) -> PushSubscriptionRepository:
    return PushSubscriptionRepository(session)


def _cipher(settings: Settings = Depends(get_settings)) -> PushSecretCipher:
    key = settings.web_push_subscription_encryption_key
    if not key:
        raise ApplicationError(
            code="WEB_PUSH_DISABLED", message="系统通知暂未启用。", status_code=503
        )
    return PushSecretCipher(key)


def _sender(settings: Settings = Depends(get_settings)) -> WebPushSender:
    return WebPushSender(settings)


def _serialize(item: PushSubscription) -> dict:
    return {
        "id": str(item.id),
        "platform": item.platform,
        "deviceLabel": item.device_label,
        "status": item.status,
        "expirationTime": item.expiration_time.isoformat() if item.expiration_time else None,
        "lastSeenAt": item.last_seen_at.isoformat() if item.last_seen_at else None,
        "lastAcceptedAt": item.last_accepted_at.isoformat() if item.last_accepted_at else None,
        "createdAt": item.created_at.isoformat(),
        "isCurrent": False,
    }


@router.get("/config")
async def get_push_config(
    settings: Settings = Depends(get_settings),
    _user_id: UUID = Depends(_get_web_user_id),
) -> dict:
    return {
        "enabled": settings.web_push_enabled,
        "applicationServerKey": (
            settings.web_push_vapid_public_key if settings.web_push_enabled else None
        ),
    }


@router.get("/subscriptions")
async def list_push_subscriptions(
    currentEndpointHash: str | None = None,
    user_id: UUID = Depends(_get_web_user_id),
    repo: PushSubscriptionRepository = Depends(_repo),
) -> dict:
    subscriptions = await repo.list_by_user(user_id)
    items = [_serialize(item) for item in subscriptions]
    if currentEndpointHash:
        for item, subscription in zip(items, subscriptions, strict=True):
            item["isCurrent"] = bool(subscription.endpoint_hash == currentEndpointHash)
    return {"items": items}


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
async def register_push_subscription(
    body: RegisterPushSubscriptionRequest,
    request: Request,
    user_id: UUID = Depends(_get_web_user_id),
    settings: Settings = Depends(get_settings),
    repo: PushSubscriptionRepository = Depends(_repo),
    cipher: PushSecretCipher = Depends(_cipher),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    if not settings.web_push_enabled:
        raise ApplicationError(
            code="WEB_PUSH_DISABLED", message="系统通知暂未启用。", status_code=503
        )
    if not idempotency_key:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED", message="缺少幂等键。", status_code=400
        )
    endpoint = validate_push_endpoint(body.endpoint, settings.web_push_allowed_endpoint_hosts)
    hashed = endpoint_hash(endpoint)
    existing = await repo.get_by_hash(hashed)
    if existing is not None and existing.user_id != user_id:
        raise ApplicationError(
            code="PUSH_SUBSCRIPTION_OWNERSHIP_CONFLICT",
            message="该通知终端已关联其他账号，请先在此终端关闭通知。",
            status_code=409,
        )
    now = datetime.now(UTC)
    item = existing or PushSubscription(user_id=user_id, endpoint_hash=hashed)
    item.endpoint_encrypted = cipher.encrypt(endpoint)
    item.p256dh_encrypted = cipher.encrypt(body.keys.p256dh)
    item.auth_encrypted = cipher.encrypt(body.keys.auth)
    item.content_encoding = body.contentEncoding
    item.expiration_time = body.expirationTime
    item.platform = body.platform
    item.device_label = body.deviceLabel
    item.user_agent = request.headers.get("user-agent", "")[:512] or None
    item.status = "active"
    item.revoked_at = None
    item.last_seen_at = now
    item.updated_at = now
    saved = await repo.save(item)
    payload = _serialize(saved)
    payload["isCurrent"] = True
    return payload


@router.delete("/subscriptions/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_push_subscription(
    subscription_id: UUID,
    user_id: UUID = Depends(_get_web_user_id),
    repo: PushSubscriptionRepository = Depends(_repo),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> None:
    if not idempotency_key:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED", message="缺少幂等键。", status_code=400
        )
    item = await repo.get(subscription_id, user_id)
    if item is None:
        raise ApplicationError(
            code="PUSH_SUBSCRIPTION_NOT_FOUND", message="未找到该通知终端。", status_code=404
        )
    await repo.revoke(item)


@router.post("/test")
async def send_test_push(
    subscriptionId: UUID,
    user_id: UUID = Depends(_get_web_user_id),
    repo: PushSubscriptionRepository = Depends(_repo),
    cipher: PushSecretCipher = Depends(_cipher),
    sender: WebPushSender = Depends(_sender),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    if not idempotency_key:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED", message="缺少幂等键。", status_code=400
        )
    item = await repo.get(subscriptionId, user_id)
    if item is None or item.status != "active":
        raise ApplicationError(
            code="PUSH_SUBSCRIPTION_NOT_FOUND", message="未找到可用的通知终端。", status_code=404
        )
    notification_id = uuid4()
    try:
        await sender.send_test(
            subscription=PushSecrets(
                endpoint=cipher.decrypt(item.endpoint_encrypted),
                p256dh=cipher.decrypt(item.p256dh_encrypted),
                auth=cipher.decrypt(item.auth_encrypted),
            ),
            notification_id=notification_id,
        )
    except ApplicationError as exc:
        if exc.code == "PUSH_SUBSCRIPTION_EXPIRED":
            await repo.revoke(item)
        raise
    item.last_accepted_at = datetime.now(UTC)
    item.failure_count = 0
    return {"notificationId": str(notification_id), "status": "accepted"}
