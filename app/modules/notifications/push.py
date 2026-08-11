import asyncio
import hashlib
import ipaddress
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from pywebpush import WebPushException, webpush
from requests import RequestException

from app.core.config import Settings
from app.core.errors import ApplicationError


@dataclass(frozen=True, slots=True)
class PushSecrets:
    endpoint: str
    p256dh: str
    auth: str


class PushSecretCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ValueError("Web Push subscription encryption key must be a Fernet key") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode()
        except (InvalidToken, ValueError) as exc:
            raise ApplicationError(
                code="PUSH_SUBSCRIPTION_UNREADABLE",
                message="通知终端凭据无法读取，请在该终端重新开启通知。",
                status_code=409,
            ) from exc


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.strip().encode()).hexdigest()


def validate_push_endpoint(endpoint: str, allowed_hosts: list[str]) -> str:
    value = endpoint.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ApplicationError(
            code="PUSH_ENDPOINT_INVALID", message="通知服务地址格式无效。", status_code=400
        ) from exc
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise ApplicationError(
            code="PUSH_ENDPOINT_INVALID",
            message="通知服务地址必须是安全的 HTTPS 地址。",
            status_code=400,
        )
    if port not in (None, 443):
        raise ApplicationError(
            code="PUSH_ENDPOINT_INVALID",
            message="通知服务地址使用了不允许的端口。",
            status_code=400,
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None:
        raise ApplicationError(
            code="PUSH_ENDPOINT_INVALID", message="通知服务地址不能使用 IP 地址。", status_code=400
        )
    allowed = any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in (item.lower().rstrip(".") for item in allowed_hosts)
    )
    if not allowed:
        raise ApplicationError(
            code="PUSH_ENDPOINT_NOT_ALLOWED",
            message="当前浏览器的通知服务暂未列入允许范围。",
            status_code=400,
            details={"host": hostname},
        )
    return value


class WebPushSender:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send_test(self, *, subscription: PushSecrets, notification_id: UUID) -> None:
        await self.send_payload(
            subscription=subscription,
            payload={
                "version": 1,
                "notificationId": str(notification_id),
                "title": "一刻通知已开启",
                "body": "这台终端可以接收提醒了。",
                "target": "/space/settings/?section=notifications",
                "tag": "moment-one-push-test",
            },
            ttl=3600,
        )

    async def send_payload(self, *, subscription: PushSecrets, payload: dict, ttl: int) -> None:
        private_key = self._settings.web_push_vapid_private_key
        subject = self._settings.web_push_vapid_subject
        if not self._settings.web_push_enabled or not private_key or not subject:
            raise ApplicationError(
                code="WEB_PUSH_DISABLED", message="系统通知暂未启用。", status_code=503
            )
        encoded_payload = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        def _send() -> None:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=encoded_payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": subject},
                ttl=ttl,
                timeout=10,
            )

        try:
            await asyncio.to_thread(_send)
        except RequestException as exc:
            raise ApplicationError(
                code="PUSH_PROVIDER_UNAVAILABLE",
                message="通知服务暂时无法连接，请稍后重试。",
                status_code=503,
                details={"occurredAt": datetime.now(UTC).isoformat()},
            ) from exc
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                raise ApplicationError(
                    code="PUSH_SUBSCRIPTION_EXPIRED",
                    message="该通知终端已失效，请重新开启通知。",
                    status_code=409,
                ) from exc
            raise ApplicationError(
                code="PUSH_DELIVERY_FAILED",
                message="通知暂时无法发送，请稍后重试。",
                status_code=502,
                details={"providerStatus": status, "occurredAt": datetime.now(UTC).isoformat()},
            ) from exc
