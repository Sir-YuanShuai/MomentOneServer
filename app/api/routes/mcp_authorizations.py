"""MCP 授权管理 API（Web 端，Casdoor Bearer 鉴权）。

- GET    /v1/mcp/authorizations        — 当前用户的 MCP 客户端授权列表
- PATCH  /v1/mcp/authorizations/{id}   — 调整 scope（下次刷新 token 生效）
- DELETE /v1/mcp/authorizations/{id}   — 撤销授权（token 立即失效）

与设备绑定管理（/v1/device/bindings）同模式，见 MCP_MVP_PLAN §2.8。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_authenticated_user_id
from app.core.errors import ApplicationError
from app.infrastructure.database.models import McpAuthorization
from app.infrastructure.database.repositories.device_repository import (
    SqlDeviceBindingRepository,
)
from app.infrastructure.database.session import get_db_session
from app.modules.mcp.scope import (
    ALL_SCOPES,
    CLIENT_TYPE_GLASSES,
    CLIENT_TYPE_MCP,
    device_id_from_client_id,
    normalize_scope_names,
)
from app.modules.mcp_oauth.repositories import McpAuthorizationRepository

router = APIRouter(prefix="/v1/mcp", tags=["mcp-authorizations"])


class AuthorizationResponse(BaseModel):
    id: str
    clientId: str
    clientName: str | None
    clientType: str
    scope: list[str]
    status: str
    lastActiveAt: str | None
    authorizedAt: str
    revokedAt: str | None


class UpdateAuthorizationRequest(BaseModel):
    scope: list[str] = Field(min_length=1, description="调整后的 scope 列表")

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, value: list[str]) -> list[str]:
        unknown = [s for s in value if s not in ALL_SCOPES]
        if unknown:
            raise ValueError(f"不支持的 scope：{', '.join(unknown)}")
        return value


def _to_response(orm: McpAuthorization) -> AuthorizationResponse:
    return AuthorizationResponse(
        id=str(orm.id),
        clientId=orm.client_id,
        clientName=orm.client_name,
        clientType=orm.client_type or CLIENT_TYPE_MCP,
        scope=orm.scope.split(),
        status=orm.status,
        lastActiveAt=orm.last_active_at.isoformat() if orm.last_active_at else None,
        authorizedAt=orm.created_at.isoformat(),
        revokedAt=orm.revoked_at.isoformat() if orm.revoked_at else None,
    )


@router.get("/authorizations", response_model=list[AuthorizationResponse])
async def list_authorizations(
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> list[AuthorizationResponse]:
    repo = McpAuthorizationRepository(session)
    rows = await repo.list_by_user(user_id)
    return [_to_response(r) for r in rows]


@router.patch("/authorizations/{authorization_id}", response_model=AuthorizationResponse)
async def update_authorization_scope(
    authorization_id: UUID,
    body: UpdateAuthorizationRequest,
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> AuthorizationResponse:
    repo = McpAuthorizationRepository(session)
    updated = await repo.update_scope(
        authorization_id=authorization_id,
        user_id=user_id,
        scope=" ".join(body.scope),
    )
    if updated is None:
        raise ApplicationError(
            code="AUTHORIZATION_NOT_FOUND",
            message="未找到该授权记录。",
            status_code=404,
        )
    # 眼镜设备：同步 device_bindings.scope legacy 镜像（统一授权记录为准）
    if updated.client_type == CLIENT_TYPE_GLASSES:
        device_id = device_id_from_client_id(updated.client_id)
        if device_id:
            binding = await SqlDeviceBindingRepository(session).get_by_device(device_id)
            if binding is not None and binding.user_id == user_id:
                await SqlDeviceBindingRepository(session).update_scope(
                    binding_id=binding.id,
                    scope=tuple(normalize_scope_names(body.scope)),
                )
    return _to_response(updated)


@router.delete("/authorizations/{authorization_id}", status_code=204)
async def revoke_authorization(
    authorization_id: UUID,
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    repo = McpAuthorizationRepository(session)
    revoked = await repo.revoke(authorization_id=authorization_id, user_id=user_id)
    if revoked is None:
        raise ApplicationError(
            code="AUTHORIZATION_NOT_FOUND",
            message="未找到该授权记录。",
            status_code=404,
        )
    # 眼镜设备：同步撤销设备绑定（token 立即失效）
    if revoked.client_type == CLIENT_TYPE_GLASSES:
        device_id = device_id_from_client_id(revoked.client_id)
        if device_id:
            binding = await SqlDeviceBindingRepository(session).get_by_device(device_id)
            if binding is not None and binding.user_id == user_id and binding.status == "active":
                from datetime import UTC, datetime

                await SqlDeviceBindingRepository(session).revoke(
                    binding_id=binding.id, revoked_at=datetime.now(UTC)
                )


@router.delete("/authorizations/{authorization_id}/record", status_code=204)
async def delete_revoked_authorization_record(
    authorization_id: UUID,
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    deleted = await McpAuthorizationRepository(session).delete_revoked(
        authorization_id=authorization_id, user_id=user_id
    )
    if not deleted:
        raise ApplicationError(
            code="REVOKED_AUTHORIZATION_NOT_FOUND",
            message="只能删除当前账号下已经撤销的授权记录。",
            status_code=404,
        )


__all__ = ["router"]
