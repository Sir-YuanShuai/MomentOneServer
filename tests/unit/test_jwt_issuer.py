"""JwtIssuer 单元测试：使用临时 RSA 密钥对，不依赖磁盘上的生产密钥。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import jwt
import pytest
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.infrastructure.jwt.issuer import JwtIssuer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
BINDING_ID = UUID("22222222-2222-4222-8222-222222222222")
DEVICE_ID = "test-device-uuid-v4"


def _generate_rsa_keypair(tmp_path: Path) -> tuple[Path, Path]:
    """生成临时 RSA 2048 密钥对并写入 PEM 文件。"""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path = tmp_path / "jwt_private.pem"
    pub_path = tmp_path / "jwt_public.pem"
    priv_path.write_bytes(priv_pem)
    pub_path.write_bytes(pub_pem)
    return priv_path, pub_path


def _make_settings(tmp_path: Path) -> Settings:
    priv_path, pub_path = _generate_rsa_keypair(tmp_path)
    return Settings(
        jwt_private_key_path=str(priv_path),
        jwt_public_key_path=str(pub_path),
        jwt_issuer="https://momentone.test",
        jwt_audience="momentone-glasses",
        access_token_ttl_seconds=3600,
        refresh_token_ttl_seconds=90 * 24 * 3600,
    )


def test_issue_access_token_payload(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    issuer = JwtIssuer(settings)
    token, expires_in = issuer.issue_access_token(
        binding_id=BINDING_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        scope=("moments:read", "moments:write"),
    )
    assert expires_in == 3600
    assert settings.jwt_public_key_path is not None
    public_key = Path(settings.jwt_public_key_path).read_text()
    payload = jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
    )
    assert payload["sub"] == str(USER_ID)
    assert payload["binding_id"] == str(BINDING_ID)
    assert payload["device_id"] == DEVICE_ID
    assert payload["scope"] == "moments:read moments:write"
    assert payload["token_type"] == "access"
    assert payload["iss"] == "https://momentone.test"


def test_issue_refresh_token_payload(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    issuer = JwtIssuer(settings)
    token = issuer.issue_refresh_token(
        binding_id=BINDING_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        scope=("moments:read",),
    )
    payload = issuer.verify_refresh_token(token)
    assert payload["token_type"] == "refresh"
    assert payload["sub"] == str(USER_ID)
    assert payload["scope"] == "moments:read"


def test_verify_refresh_token_rejects_access_token(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    issuer = JwtIssuer(settings)
    access_token, _ = issuer.issue_access_token(
        binding_id=BINDING_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        scope=("moments:read",),
    )
    with pytest.raises(ApplicationError) as exc_info:
        issuer.verify_refresh_token(access_token)
    assert exc_info.value.code == "REFRESH_TOKEN_INVALID"
    assert exc_info.value.status_code == 401


def test_verify_refresh_token_rejects_tampered_token(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    issuer = JwtIssuer(settings)
    token = issuer.issue_refresh_token(
        binding_id=BINDING_ID,
        user_id=USER_ID,
        device_id=DEVICE_ID,
        scope=("moments:read",),
    )
    # 篡改签名
    tampered = token[:-8] + "AAAAAAAA"
    with pytest.raises(ApplicationError) as exc_info:
        issuer.verify_refresh_token(tampered)
    assert exc_info.value.code == "REFRESH_TOKEN_INVALID"


def test_verify_refresh_token_rejects_expired(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    issuer = JwtIssuer(settings)
    # 手动构造一个已过期的 refresh token
    now = datetime.now(UTC)
    payload = {
        "iss": settings.jwt_issuer,
        "sub": str(USER_ID),
        "aud": settings.jwt_audience,
        "iat": int((now - timedelta(days=100)).timestamp()),
        "exp": int((now - timedelta(days=10)).timestamp()),
        "binding_id": str(BINDING_ID),
        "device_id": DEVICE_ID,
        "scope": "moments:read",
        "token_type": "refresh",
    }
    assert settings.jwt_private_key_path is not None
    priv_key = Path(settings.jwt_private_key_path).read_text(encoding="utf-8")
    expired_token = jwt.encode(payload, priv_key, algorithm="RS256")
    with pytest.raises(ApplicationError) as exc_info:
        issuer.verify_refresh_token(expired_token)
    assert exc_info.value.code == "REFRESH_TOKEN_INVALID"


def test_ensure_keys_raises_when_not_configured() -> None:
    settings = Settings(
        jwt_private_key_path=None,
        jwt_public_key_path=None,
        jwt_issuer="https://momentone.test",
        jwt_audience="momentone-glasses",
        access_token_ttl_seconds=3600,
        refresh_token_ttl_seconds=90 * 24 * 3600,
    )
    issuer = JwtIssuer(settings)
    with pytest.raises(ApplicationError) as exc_info:
        issuer.issue_access_token(
            binding_id=BINDING_ID,
            user_id=USER_ID,
            device_id=DEVICE_ID,
            scope=("moments:read",),
        )
    assert exc_info.value.code == "JWT_NOT_CONFIGURED"
    assert exc_info.value.status_code == 500
