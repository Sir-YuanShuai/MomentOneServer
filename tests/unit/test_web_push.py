from app.core.errors import ApplicationError
from app.modules.notifications.push import PushSecretCipher, endpoint_hash, validate_push_endpoint
from cryptography.fernet import Fernet
from fastapi import status


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
