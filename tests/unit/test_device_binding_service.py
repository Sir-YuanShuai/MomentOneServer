"""DeviceBindingService 单元测试：用内存 Fake Repository，不依赖数据库。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.devices.domain import (
    BindingCode,
    BindingCodeStatus,
    BindingStatus,
    Device,
    DeviceBinding,
)
from app.modules.devices.service import DeviceBindingService
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = UUID("33333333-3333-4333-8333-333333333333")
DEVICE_ID = "device-aaa-bbb-ccc"


def _generate_rsa_keypair(tmp_path: Path) -> tuple[Path, Path]:
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
        binding_code_ttl_seconds=300,
        binding_code_length=24,
    )


class FakeDeviceRepository:
    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}

    async def get_or_create(
        self,
        *,
        device_id: str,
        device_type: str | None,
        device_name: str | None,
    ) -> Device:
        if device_id in self.devices:
            return self.devices[device_id]
        device = Device(
            id=device_id,
            device_type=device_type,
            device_name=device_name,
            created_at=datetime.now(UTC),
        )
        self.devices[device_id] = device
        return device

    async def get(self, device_id: str) -> Device | None:
        return self.devices.get(device_id)


class _FakeAuthRecord:
    """授权记录替身（属性访问兼容 service 层 authz.scope 用法）。"""

    def __init__(
        self,
        *,
        user_id: UUID,
        client_id: str,
        client_name: str | None,
        client_type: str,
        scope: str,
    ) -> None:
        self.id = uuid4()
        self.user_id = user_id
        self.client_id = client_id
        self.client_name = client_name
        self.client_type = client_type
        self.scope = scope
        self.status = "active"


class FakeAuthorizationRepository:
    """统一授权记录（mcp_authorizations）内存替身。"""

    def __init__(self) -> None:
        self.records: dict[tuple[UUID, str], _FakeAuthRecord] = {}

    async def upsert(
        self,
        *,
        user_id: UUID,
        client_id: str,
        client_name: str | None,
        scope: str,
        client_type: str = "mcp",
    ) -> _FakeAuthRecord:
        key = (user_id, client_id)
        existing = self.records.get(key)
        if existing is not None and existing.status == "active":
            # 保留用户已配置 scope（与真实 repo 语义一致）
            existing.client_name = client_name or existing.client_name
            existing.client_type = client_type
            return existing
        record = _FakeAuthRecord(
            user_id=user_id,
            client_id=client_id,
            client_name=client_name,
            client_type=client_type,
            scope=scope,
        )
        self.records[key] = record
        return record

    async def get_by_user_and_client(self, user_id: UUID, client_id: str) -> _FakeAuthRecord | None:
        return self.records.get((user_id, client_id))

    async def update_scope_by_client(
        self, *, user_id: UUID, client_id: str, scope: str
    ) -> _FakeAuthRecord | None:
        record = self.records.get((user_id, client_id))
        if record is None:
            return None
        record.scope = scope
        return record

    async def revoke_by_client(self, *, user_id: UUID, client_id: str) -> _FakeAuthRecord | None:
        record = self.records.get((user_id, client_id))
        if record is None:
            return None
        record.status = "revoked"
        return record


class FakeBindingRepository:
    def __init__(self) -> None:
        self.bindings: dict[UUID, DeviceBinding] = {}
        self._by_device: dict[str, UUID] = {}

    async def create(
        self,
        *,
        user_id: UUID,
        device_id: str,
        scope: tuple[str, ...],
        refresh_token_hash: str,
    ) -> DeviceBinding:
        binding_id = uuid4()
        binding = DeviceBinding(
            id=binding_id,
            user_id=user_id,
            device_id=device_id,
            scope=scope,
            status=BindingStatus.ACTIVE,
            refresh_token_hash=refresh_token_hash,
            bound_at=datetime.now(UTC),
            last_active_at=None,
            revoked_at=None,
        )
        self.bindings[binding_id] = binding
        self._by_device[device_id] = binding_id
        return binding

    async def get(self, binding_id: UUID) -> DeviceBinding | None:
        return self.bindings.get(binding_id)

    async def get_by_device(self, device_id: str) -> DeviceBinding | None:
        bid = self._by_device.get(device_id)
        if bid is None:
            return None
        return self.bindings.get(bid)

    async def list_by_user(self, user_id: UUID) -> list[DeviceBinding]:
        return [b for b in self.bindings.values() if b.user_id == user_id]

    async def update_refresh_token_hash(
        self,
        *,
        binding_id: UUID,
        refresh_token_hash: str,
        last_active_at: datetime,
    ) -> None:
        b = self.bindings[binding_id]
        self.bindings[binding_id] = DeviceBinding(
            id=b.id,
            user_id=b.user_id,
            device_id=b.device_id,
            scope=b.scope,
            status=b.status,
            refresh_token_hash=refresh_token_hash,
            bound_at=b.bound_at,
            last_active_at=last_active_at,
            revoked_at=b.revoked_at,
        )

    async def revoke(self, *, binding_id: UUID, revoked_at: datetime) -> None:
        b = self.bindings[binding_id]
        self.bindings[binding_id] = DeviceBinding(
            id=b.id,
            user_id=b.user_id,
            device_id=b.device_id,
            scope=b.scope,
            status=BindingStatus.REVOKED,
            refresh_token_hash=b.refresh_token_hash,
            bound_at=b.bound_at,
            last_active_at=b.last_active_at,
            revoked_at=revoked_at,
        )

    async def update_scope(self, *, binding_id: UUID, scope: tuple[str, ...]) -> DeviceBinding:
        b = self.bindings[binding_id]
        updated = DeviceBinding(
            id=b.id,
            user_id=b.user_id,
            device_id=b.device_id,
            scope=scope,
            status=b.status,
            refresh_token_hash=b.refresh_token_hash,
            bound_at=b.bound_at,
            last_active_at=b.last_active_at,
            revoked_at=b.revoked_at,
        )
        self.bindings[binding_id] = updated
        return updated


class FakeBindingCodeRepository:
    def __init__(self) -> None:
        self.codes: dict[UUID, BindingCode] = {}
        self._by_code: dict[str, UUID] = {}

    async def create(
        self,
        *,
        user_id: UUID,
        code: str,
        scope: tuple[str, ...],
        device_name: str | None,
        expires_at: datetime,
    ) -> BindingCode:
        code_id = uuid4()
        bc = BindingCode(
            id=code_id,
            code=code,
            user_id=user_id,
            scope=scope,
            device_name=device_name,
            status=BindingCodeStatus.PENDING,
            expires_at=expires_at,
            used_at=None,
            created_at=datetime.now(UTC),
        )
        self.codes[code_id] = bc
        self._by_code[code] = code_id
        return bc

    async def get_by_code(self, code: str) -> BindingCode | None:
        cid = self._by_code.get(code)
        if cid is None:
            return None
        return self.codes.get(cid)

    async def mark_used(self, *, code_id: UUID, used_at: datetime) -> None:
        bc = self.codes[code_id]
        self.codes[code_id] = BindingCode(
            id=bc.id,
            code=bc.code,
            user_id=bc.user_id,
            scope=bc.scope,
            device_name=bc.device_name,
            status=BindingCodeStatus.USED,
            expires_at=bc.expires_at,
            used_at=used_at,
            created_at=bc.created_at,
        )


def _make_service(
    tmp_path: Path,
) -> tuple[
    DeviceBindingService,
    FakeBindingRepository,
    FakeBindingCodeRepository,
    FakeDeviceRepository,
    JwtIssuer,
    Settings,
    FakeAuthorizationRepository,
]:
    settings = _make_settings(tmp_path)
    issuer = JwtIssuer(settings)
    bindings = FakeBindingRepository()
    codes = FakeBindingCodeRepository()
    devices = FakeDeviceRepository()
    authorizations = FakeAuthorizationRepository()
    service = DeviceBindingService(
        bindings=bindings,
        codes=codes,
        devices=devices,
        jwt_issuer=issuer,
        settings=settings,
        authorizations=authorizations,  # type: ignore[arg-type]
    )
    return service, bindings, codes, devices, issuer, settings, authorizations


@pytest.mark.asyncio
async def test_create_binding_session_returns_pending_code(tmp_path: Path) -> None:
    service, _, codes, *_unused = _make_service(tmp_path)
    bc = await service.create_binding_session(user_id=USER_ID)
    assert bc.user_id == USER_ID
    assert bc.status == BindingCodeStatus.PENDING
    assert bc.code.startswith("BIND-")
    assert bc.scope == ("moments.read", "moments.write")
    assert codes.codes[bc.id].code == bc.code


@pytest.mark.asyncio
async def test_complete_binding_success(tmp_path: Path) -> None:
    service, bindings, codes, devices, *_unused = _make_service(tmp_path)
    bc = await service.create_binding_session(user_id=USER_ID)
    resp = await service.complete_binding(
        binding_code=bc.code,
        device_id=DEVICE_ID,
        device_name="Rokid",
        device_type="glasses",
    )
    assert resp.token_type == "Bearer"
    assert resp.expires_in == 3600
    assert resp.scope == "moments.read moments.write"
    # binding 已创建
    binding = await bindings.get(resp.binding_id)
    assert binding is not None
    assert binding.user_id == USER_ID
    assert binding.device_id == DEVICE_ID
    assert binding.status == BindingStatus.ACTIVE
    # device 已注册
    device = await devices.get(DEVICE_ID)
    assert device is not None
    assert device.device_name == "Rokid"
    # code 已标记为 USED
    used_bc = await codes.get_by_code(bc.code)
    assert used_bc is not None
    assert used_bc.status == BindingCodeStatus.USED


@pytest.mark.asyncio
async def test_complete_binding_invalid_code(tmp_path: Path) -> None:
    service, *_unused = _make_service(tmp_path)
    with pytest.raises(ApplicationError) as exc_info:
        await service.complete_binding(
            binding_code="BIND-not-exist",
            device_id=DEVICE_ID,
            device_name=None,
            device_type=None,
        )
    assert exc_info.value.code == "BINDING_CODE_INVALID"
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_complete_binding_used_code(tmp_path: Path) -> None:
    service, *_unused = _make_service(tmp_path)
    bc = await service.create_binding_session(user_id=USER_ID)
    await service.complete_binding(
        binding_code=bc.code,
        device_id=DEVICE_ID,
        device_name=None,
        device_type=None,
    )
    # 第二次使用同一 code
    with pytest.raises(ApplicationError) as exc_info:
        await service.complete_binding(
            binding_code=bc.code,
            device_id="device-other",
            device_name=None,
            device_type=None,
        )
    assert exc_info.value.code == "BINDING_CODE_USED"


@pytest.mark.asyncio
async def test_complete_binding_expired_code(tmp_path: Path) -> None:
    service, _, codes, *_unused = _make_service(tmp_path)
    bc = await service.create_binding_session(user_id=USER_ID)
    # 手动把 expires_at 设为过去
    expired = BindingCode(
        id=bc.id,
        code=bc.code,
        user_id=bc.user_id,
        scope=bc.scope,
        device_name=bc.device_name,
        status=BindingCodeStatus.PENDING,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        used_at=None,
        created_at=bc.created_at,
    )
    codes.codes[bc.id] = expired
    with pytest.raises(ApplicationError) as exc_info:
        await service.complete_binding(
            binding_code=bc.code,
            device_id=DEVICE_ID,
            device_name=None,
            device_type=None,
        )
    assert exc_info.value.code == "BINDING_CODE_EXPIRED"


@pytest.mark.asyncio
async def test_complete_binding_device_already_bound_other_user(tmp_path: Path) -> None:
    service, *_unused = _make_service(tmp_path)
    # 用户 A 先绑定设备
    bc_a = await service.create_binding_session(user_id=USER_ID)
    await service.complete_binding(
        binding_code=bc_a.code,
        device_id=DEVICE_ID,
        device_name=None,
        device_type=None,
    )
    # 用户 B 尝试绑定同一设备
    bc_b = await service.create_binding_session(user_id=OTHER_USER_ID)
    with pytest.raises(ApplicationError) as exc_info:
        await service.complete_binding(
            binding_code=bc_b.code,
            device_id=DEVICE_ID,
            device_name=None,
            device_type=None,
        )
    assert exc_info.value.code == "DEVICE_ALREADY_BOUND"
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_complete_binding_same_user_rebind_revokes_old(tmp_path: Path) -> None:
    service, bindings, *_unused = _make_service(tmp_path)
    bc1 = await service.create_binding_session(user_id=USER_ID)
    resp1 = await service.complete_binding(
        binding_code=bc1.code,
        device_id=DEVICE_ID,
        device_name=None,
        device_type=None,
    )
    bc2 = await service.create_binding_session(user_id=USER_ID)
    resp2 = await service.complete_binding(
        binding_code=bc2.code,
        device_id=DEVICE_ID,
        device_name=None,
        device_type=None,
    )
    old = await bindings.get(resp1.binding_id)
    assert old is not None
    assert old.status == BindingStatus.REVOKED
    new = await bindings.get(resp2.binding_id)
    assert new is not None
    assert new.status == BindingStatus.ACTIVE


@pytest.mark.asyncio
async def test_refresh_access_token_success(tmp_path: Path) -> None:
    service, *_unused = _make_service(tmp_path)
    bc = await service.create_binding_session(user_id=USER_ID)
    resp = await service.complete_binding(
        binding_code=bc.code,
        device_id=DEVICE_ID,
        device_name=None,
        device_type=None,
    )
    refreshed = await service.refresh_access_token(resp.refresh_token)
    assert refreshed.binding_id == resp.binding_id
    assert refreshed.token_type == "Bearer"
    assert refreshed.expires_in == 3600
    assert refreshed.scope == resp.scope
    # 新 refresh_token 可用于下一次刷新（滚动续期链路通）
    await service.refresh_access_token(refreshed.refresh_token)


@pytest.mark.asyncio
async def test_refresh_access_token_revoked_binding_fails(tmp_path: Path) -> None:
    service, *_unused = _make_service(tmp_path)
    bc = await service.create_binding_session(user_id=USER_ID)
    resp = await service.complete_binding(
        binding_code=bc.code,
        device_id=DEVICE_ID,
        device_name=None,
        device_type=None,
    )
    await service.revoke_binding(user_id=USER_ID, binding_id=resp.binding_id)
    with pytest.raises(ApplicationError) as exc_info:
        await service.refresh_access_token(resp.refresh_token)
    assert exc_info.value.code == "REFRESH_TOKEN_INVALID"


@pytest.mark.asyncio
async def test_revoke_binding_not_found(tmp_path: Path) -> None:
    service, *_unused = _make_service(tmp_path)
    with pytest.raises(ApplicationError) as exc_info:
        await service.revoke_binding(user_id=USER_ID, binding_id=uuid4())
    assert exc_info.value.code == "BINDING_NOT_FOUND"
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_revoke_binding_other_user_returns_not_found(tmp_path: Path) -> None:
    service, *_unused = _make_service(tmp_path)
    bc = await service.create_binding_session(user_id=USER_ID)
    resp = await service.complete_binding(
        binding_code=bc.code,
        device_id=DEVICE_ID,
        device_name=None,
        device_type=None,
    )
    with pytest.raises(ApplicationError) as exc_info:
        await service.revoke_binding(user_id=OTHER_USER_ID, binding_id=resp.binding_id)
    assert exc_info.value.code == "BINDING_NOT_FOUND"


@pytest.mark.asyncio
async def test_revoke_binding_already_revoked(tmp_path: Path) -> None:
    service, *_unused = _make_service(tmp_path)
    bc = await service.create_binding_session(user_id=USER_ID)
    resp = await service.complete_binding(
        binding_code=bc.code,
        device_id=DEVICE_ID,
        device_name=None,
        device_type=None,
    )
    await service.revoke_binding(user_id=USER_ID, binding_id=resp.binding_id)
    with pytest.raises(ApplicationError) as exc_info:
        await service.revoke_binding(user_id=USER_ID, binding_id=resp.binding_id)
    assert exc_info.value.code == "BINDING_ALREADY_REVOKED"
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_update_scope_success(tmp_path: Path) -> None:
    service, *_unused = _make_service(tmp_path)
    bc = await service.create_binding_session(user_id=USER_ID)
    resp = await service.complete_binding(
        binding_code=bc.code,
        device_id=DEVICE_ID,
        device_name=None,
        device_type=None,
    )
    updated = await service.update_scope(
        user_id=USER_ID,
        binding_id=resp.binding_id,
        scope=("moments.read",),
    )
    assert updated.scope == ("moments.read",)


@pytest.mark.asyncio
async def test_list_user_bindings(tmp_path: Path) -> None:
    service, *_unused = _make_service(tmp_path)
    bc = await service.create_binding_session(user_id=USER_ID)
    await service.complete_binding(
        binding_code=bc.code,
        device_id=DEVICE_ID,
        device_name=None,
        device_type=None,
    )
    result = await service.list_user_bindings(USER_ID)
    assert len(result) == 1
    assert result[0].user_id == USER_ID
    # 其他用户查不到
    other = await service.list_user_bindings(OTHER_USER_ID)
    assert other == []
