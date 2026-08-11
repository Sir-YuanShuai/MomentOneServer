from uuid import uuid4

import pytest
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.modules.notifications import push as push_module
from app.modules.notifications.push import (
    PushSecretCipher,
    PushSecrets,
    WebPushSender,
    endpoint_hash,
    validate_push_endpoint,
)
from cryptography.fernet import Fernet
from fastapi import status
from requests import ConnectionError as RequestsConnectionError


def test_push_secret_cipher_round_trip() -> None:
    cipher = PushSecretCipher(Fernet.generate_key().decode())
    encrypted = cipher.encrypt("https://fcm.googleapis.com/example")
    assert encrypted != "https://fcm.googleapis.com/example"
    assert cipher.decrypt(encrypted) == "https://fcm.googleapis.com/example"


def test_endpoint_hash_is_stable_and_opaque() -> None:
    endpoint = "https://fcm.googleapis.com/example"
    assert endpoint_hash(endpoint) == endpoint_hash(endpoint)
    assert endpoint not in endpoint_hash(endpoint)
    assert len(endpoint_hash(endpoint)) == 64


def test_validate_push_endpoint_accepts_configured_service() -> None:
    endpoint = "https://fcm.googleapis.com/fcm/send/example"
    assert validate_push_endpoint(endpoint, ["fcm.googleapis.com"]) == endpoint


def test_validate_push_endpoint_rejects_ssrf_targets() -> None:
    rejected = [
        "http://fcm.googleapis.com/example",
        "https://127.0.0.1/example",
        "https://user:pass@fcm.googleapis.com/example",
        "https://fcm.googleapis.com:8443/example",
        "https://internal.example/example",
    ]
    for endpoint in rejected:
        try:
            validate_push_endpoint(endpoint, ["fcm.googleapis.com"])
        except ApplicationError as exc:
            assert exc.status_code == status.HTTP_400_BAD_REQUEST
        else:
            raise AssertionError(f"unsafe endpoint accepted: {endpoint}")


@pytest.mark.asyncio
async def test_sender_maps_provider_network_failure_to_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        web_push_enabled=True,
        web_push_vapid_public_key="public-key",
        web_push_vapid_private_key="private-key",
        web_push_vapid_subject="mailto:ops@example.com",
        web_push_subscription_encryption_key=Fernet.generate_key().decode(),
    )

    def fail_delivery(**_kwargs: object) -> None:
        raise RequestsConnectionError("network unavailable")

    monkeypatch.setattr(push_module, "webpush", fail_delivery)

    with pytest.raises(ApplicationError) as caught:
        await WebPushSender(settings).send_test(
            subscription=PushSecrets(
                endpoint="https://fcm.googleapis.com/example",
                p256dh="test-p256dh",
                auth="test-auth",
            ),
            notification_id=uuid4(),
        )

    assert caught.value.code == "PUSH_PROVIDER_UNAVAILABLE"
    assert caught.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
