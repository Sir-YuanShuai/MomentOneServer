"""设备绑定 Service 层。

业务流程：
1. create_binding_session: Web 端用户创建绑定会话，生成 binding_code
2. complete_binding: 眼镜端扫码后用 binding_code 换 token
3. refresh_access_token: access_token 过期后用 refresh_token 刷新（滚动续期）
4. list_user_bindings / revoke_binding / update_scope: Web 端管理
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.infrastructure.binding_codes.generator import generate_binding_code
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.devices.domain import (
    BindingCode,
    BindingCodeStatus,
    BindingStatus,
    DeviceBinding,
    TokenResponse,
)
from app.modules.devices.repository import (
    BindingCodeRepository,
    DeviceBindingRepository,
    DeviceRepository,
)


def _hash_token(token: str) -> str:
    """SHA-256 哈希 refresh_token，不存明文。"""
    return sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


class DeviceBindingService:
    def __init__(
        self,
        *,
        bindings: DeviceBindingRepository,
        codes: BindingCodeRepository,
        devices: DeviceRepository,
        jwt_issuer: JwtIssuer,
        settings: Settings,
    ) -> None:
        self._bindings = bindings
        self._codes = codes
        self._devices = devices
        self._jwt_issuer = jwt_issuer
        self._settings = settings

    async def create_binding_session(
        self,
        *,
        user_id: UUID,
        scope: tuple[str, ...] | None = None,
        device_name: str | None = None,
    ) -> BindingCode:
        """Web 端创建绑定会话，生成 binding_code（5 分钟过期）。"""
        resolved_scope = scope or ("moments.read", "moments.write")
        code = generate_binding_code(self._settings.binding_code_length)
        expires_at = _now() + timedelta(seconds=self._settings.binding_code_ttl_seconds)
        return await self._codes.create(
            user_id=user_id,
            code=code,
            scope=resolved_scope,
            device_name=device_name,
            expires_at=expires_at,
        )

    async def complete_binding(
        self,
        *,
        binding_code: str,
        device_id: str,
        device_name: str | None,
        device_type: str | None,
    ) -> TokenResponse:
        """眼镜端扫码后用 binding_code 换 token。

        步骤：验证 code → 检查设备未绑定其他用户 → 注册设备 → 创建 binding
        → 签发 token → 标记 code 已用
        """
        code = await self._codes.get_by_code(binding_code)
        if code is None:
            raise ApplicationError(
                code="BINDING_CODE_INVALID",
                message="binding_code 不存在或格式错误。",
                status_code=400,
            )
        if code.status == BindingCodeStatus.USED:
            raise ApplicationError(
                code="BINDING_CODE_USED",
                message="binding_code 已被使用。",
                status_code=400,
            )
        if code.status == BindingCodeStatus.EXPIRED or _now() > code.expires_at:
            raise ApplicationError(
                code="BINDING_CODE_EXPIRED",
                message="binding_code 已过期。",
                status_code=400,
            )

        # 检查设备是否已绑定其他用户
        existing = await self._bindings.get_by_device(device_id)
        if existing is not None and existing.user_id != code.user_id:
            raise ApplicationError(
                code="DEVICE_ALREADY_BOUND",
                message="此设备已绑定到其他账号。",
                status_code=409,
            )
        # 同一用户重复绑定：撤销旧 binding，允许重新绑定
        if existing is not None and existing.user_id == code.user_id:
            await self._bindings.revoke(binding_id=existing.id, revoked_at=_now())

        # 注册设备（upsert）
        await self._devices.get_or_create(
            device_id=device_id,
            device_type=device_type,
            device_name=device_name,
        )

        # 签发 token
        access_token, expires_in = self._jwt_issuer.issue_access_token(
            binding_id=UUID("00000000-0000-0000-0000-000000000000"),  # 占位，创建后重签
            user_id=code.user_id,
            device_id=device_id,
            scope=code.scope,
        )
        refresh_token = self._jwt_issuer.issue_refresh_token(
            binding_id=UUID("00000000-0000-0000-0000-000000000000"),
            user_id=code.user_id,
            device_id=device_id,
            scope=code.scope,
        )

        # 创建 binding（存 refresh_token_hash）
        binding = await self._bindings.create(
            user_id=code.user_id,
            device_id=device_id,
            scope=code.scope,
            refresh_token_hash=_hash_token(refresh_token),
        )

        # 用真实 binding_id 重签 token
        access_token, expires_in = self._jwt_issuer.issue_access_token(
            binding_id=binding.id,
            user_id=code.user_id,
            device_id=device_id,
            scope=code.scope,
        )
        refresh_token = self._jwt_issuer.issue_refresh_token(
            binding_id=binding.id,
            user_id=code.user_id,
            device_id=device_id,
            scope=code.scope,
        )
        await self._bindings.update_refresh_token_hash(
            binding_id=binding.id,
            refresh_token_hash=_hash_token(refresh_token),
            last_active_at=_now(),
        )

        # 标记 code 已用
        await self._codes.mark_used(code_id=code.id, used_at=_now())

        return TokenResponse(
            binding_id=binding.id,
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="Bearer",
            expires_in=expires_in,
            scope=" ".join(code.scope),
        )

    async def refresh_access_token(self, refresh_token: str) -> TokenResponse:
        """用 refresh_token 刷新 access_token（滚动续期）。"""
        payload = self._jwt_issuer.verify_refresh_token(refresh_token)
        binding_id = UUID(payload["binding_id"])
        user_id = UUID(payload["sub"])
        device_id = payload["device_id"]
        scope = tuple(payload.get("scope", "").split())

        binding = await self._bindings.get(binding_id)
        if binding is None:
            raise ApplicationError(
                code="REFRESH_TOKEN_INVALID",
                message="绑定关系不存在。",
                status_code=401,
            )
        if binding.status != BindingStatus.ACTIVE:
            raise ApplicationError(
                code="REFRESH_TOKEN_INVALID",
                message="绑定已撤销。",
                status_code=401,
            )
        # 校验 refresh_token_hash 匹配（防止旧 token 被重放）
        if binding.refresh_token_hash != _hash_token(refresh_token):
            raise ApplicationError(
                code="REFRESH_TOKEN_INVALID",
                message="refresh_token 已失效。",
                status_code=401,
            )

        # 签发新 access_token + 新 refresh_token（滚动续期）
        access_token, expires_in = self._jwt_issuer.issue_access_token(
            binding_id=binding.id,
            user_id=user_id,
            device_id=device_id,
            scope=scope,
        )
        new_refresh_token = self._jwt_issuer.issue_refresh_token(
            binding_id=binding.id,
            user_id=user_id,
            device_id=device_id,
            scope=scope,
        )
        await self._bindings.update_refresh_token_hash(
            binding_id=binding.id,
            refresh_token_hash=_hash_token(new_refresh_token),
            last_active_at=_now(),
        )

        return TokenResponse(
            binding_id=binding.id,
            access_token=access_token,
            refresh_token=new_refresh_token,
            token_type="Bearer",
            expires_in=expires_in,
            scope=" ".join(scope),
        )

    async def list_user_bindings(self, user_id: UUID) -> list[DeviceBinding]:
        return await self._bindings.list_by_user(user_id)

    async def revoke_binding(self, *, user_id: UUID, binding_id: UUID) -> None:
        binding = await self._bindings.get(binding_id)
        if binding is None:
            raise ApplicationError(
                code="BINDING_NOT_FOUND",
                message="绑定关系不存在。",
                status_code=404,
            )
        if binding.user_id != user_id:
            raise ApplicationError(
                code="BINDING_NOT_FOUND",
                message="绑定关系不存在。",
                status_code=404,
            )
        if binding.status == BindingStatus.REVOKED:
            raise ApplicationError(
                code="BINDING_ALREADY_REVOKED",
                message="绑定已被撤销。",
                status_code=409,
            )
        await self._bindings.revoke(binding_id=binding_id, revoked_at=_now())

    async def update_scope(
        self, *, user_id: UUID, binding_id: UUID, scope: tuple[str, ...]
    ) -> DeviceBinding:
        binding = await self._bindings.get(binding_id)
        if binding is None:
            raise ApplicationError(
                code="BINDING_NOT_FOUND",
                message="绑定关系不存在。",
                status_code=404,
            )
        if binding.user_id != user_id:
            raise ApplicationError(
                code="BINDING_NOT_FOUND",
                message="绑定关系不存在。",
                status_code=404,
            )
        return await self._bindings.update_scope(binding_id=binding_id, scope=scope)
