import asyncio
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.core.request_context import request_id_context
from app.infrastructure.database.repositories.audit_event_repository import SqlAuditEventRepository
from app.infrastructure.database.repositories.idempotency_repository import (
    SqlIdempotencyRepository,
    fingerprint_payload,
)
from app.infrastructure.database.session import get_db_session
from app.infrastructure.storage.object_storage import ObjectStorageNotConfigured, S3ObjectStorage
from app.modules.admin.auth import AdminContext, get_admin_context
from app.modules.admin.domain import AdminAuditEvent, AdminAuthorization, AdminBinding, AdminUser
from app.modules.admin.entitlements import AdminEntitlementRepository
from app.modules.admin.repository import AdminRepository

router = APIRouter(prefix="/v1/admin", tags=["admin"])


class AdminSessionResponse(BaseModel):
    isAdmin: bool = True
    isSuperAdmin: bool
    permissions: list[str]
    source: str
    identity: dict[str, str | None]


class PageResponse(BaseModel):
    items: list[dict[str, object]]
    nextCursor: str | None = None
    hasMore: bool = False


class UserStatusRequest(BaseModel):
    status: str = Field(pattern="^(active|disabled)$")
    expectedRevision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=240)


class RevokeRequest(BaseModel):
    expectedRevision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=240)


class MaintenanceConfirmRequest(BaseModel):
    previewedAt: datetime


class SetPlanRequest(BaseModel):
    planKey: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    expectedRevision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=240)


class AddStorageGrantRequest(BaseModel):
    quotaBytes: int = Field(gt=0, le=10 * 1024 * 1024 * 1024 * 1024)
    expiresAt: datetime | None = None
    expectedRevision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=240)


class ReconcileStorageRequest(BaseModel):
    expectedRevision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=240)


def _admin_repo(session: AsyncSession = Depends(get_db_session)) -> AdminRepository:
    return AdminRepository(session)


def _admin_entitlement_repo(
    session: AsyncSession = Depends(get_db_session),
) -> AdminEntitlementRepository:
    return AdminEntitlementRepository(session)


def _user_dict(user: AdminUser) -> dict[str, object]:
    return {
        "id": str(user.id),
        "displayName": user.display_name,
        "email": user.email,
        "casdoorSub": user.casdoor_sub,
        "status": user.status,
        "revision": user.revision,
        "createdAt": user.created_at.isoformat(),
        "updatedAt": user.updated_at.isoformat(),
        "lastActiveAt": user.last_active_at.isoformat() if user.last_active_at else None,
        "disabledAt": user.disabled_at.isoformat() if user.disabled_at else None,
        "disableReason": user.disable_reason,
        "momentCount": user.moment_count,
        "activeBindingCount": user.active_binding_count,
        "activeMcpCount": user.active_mcp_count,
    }


def _binding_dict(item: AdminBinding) -> dict[str, object]:
    return {
        "id": str(item.id),
        "userId": str(item.user_id),
        "userDisplayName": item.user_display_name,
        "userEmail": item.user_email,
        "deviceId": item.device_id,
        "deviceName": item.device_name,
        "scope": list(item.scope),
        "status": item.status,
        "revision": item.revision,
        "boundAt": item.bound_at.isoformat(),
        "lastActiveAt": item.last_active_at.isoformat() if item.last_active_at else None,
        "revokedAt": item.revoked_at.isoformat() if item.revoked_at else None,
    }


def _authorization_dict(item: AdminAuthorization) -> dict[str, object]:
    return {
        "id": str(item.id),
        "userId": str(item.user_id),
        "userDisplayName": item.user_display_name,
        "userEmail": item.user_email,
        "clientId": item.client_id,
        "clientName": item.client_name,
        "clientType": item.client_type,
        "scope": list(item.scope),
        "status": item.status,
        "revision": item.revision,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
        "lastActiveAt": item.last_active_at.isoformat() if item.last_active_at else None,
        "revokedAt": item.revoked_at.isoformat() if item.revoked_at else None,
    }


def _audit_dict(item: AdminAuditEvent) -> dict[str, object]:
    return {
        "id": str(item.id),
        "userId": str(item.user_id) if item.user_id else None,
        "actorType": item.actor_type,
        "actorId": item.actor_id,
        "eventType": item.event_type,
        "resourceType": item.resource_type,
        "resourceId": str(item.resource_id) if item.resource_id else None,
        "requestId": item.request_id,
        "allowed": item.allowed,
        "reason": item.reason,
        "metadata": item.metadata,
        "createdAt": item.created_at.isoformat(),
    }


async def _idempotency(
    *,
    session: AsyncSession,
    admin: AdminContext,
    operation: str,
    key: str | None,
    payload: Mapping[str, object],
):
    if not key:
        raise ApplicationError(
            code="INVALID_REQUEST", message="管理写操作必须提供 Idempotency-Key。", status_code=400
        )
    repo = SqlIdempotencyRepository(session)
    record = await repo.acquire(
        user_id=admin.user_id,
        operation=operation,
        idempotency_key=key,
        request_payload=dict(payload),
    )
    if record.request_fingerprint != fingerprint_payload(dict(payload)):
        raise ApplicationError(
            code="IDEMPOTENCY_CONFLICT",
            message="Idempotency-Key 已用于不同的请求。",
            status_code=409,
        )
    return repo, record


async def _audit(
    session: AsyncSession,
    admin: AdminContext,
    *,
    event_type: str,
    resource_type: str,
    resource_id: UUID | None,
    allowed: bool,
    reason: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> None:
    await SqlAuditEventRepository(session).append(
        user_id=admin.user_id,
        actor_type="admin",
        actor_id=admin.subject,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id_context.get(),
        allowed=allowed,
        reason=reason,
        metadata=dict(metadata) if metadata else None,
    )


@router.get("/session", response_model=AdminSessionResponse)
async def admin_session(admin: AdminContext = Depends(get_admin_context)) -> AdminSessionResponse:
    admin.require("admin.read")
    return AdminSessionResponse(
        isSuperAdmin=admin.is_super_admin,
        permissions=sorted(admin.permissions),
        source=admin.source,
        identity={
            "subject": admin.subject,
            "displayName": admin.display_name,
            "email": admin.email,
        },
    )


@router.get("/overview")
async def overview(
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminRepository = Depends(_admin_repo),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    admin.require("admin.read")
    data = await repo.overview()
    dependencies: dict[str, object] = {}
    started = time.perf_counter()
    try:
        await session.scalar(text("SELECT 1"))
        dependencies["database"] = {
            "status": "ok",
            "latencyMs": round((time.perf_counter() - started) * 1000),
        }
    except Exception:
        dependencies["database"] = {"status": "error", "latencyMs": None}
    started = time.perf_counter()
    try:
        storage = S3ObjectStorage(settings)
        await asyncio.to_thread(storage.check_health)
        dependencies["objectStorage"] = {
            "status": "ok",
            "latencyMs": round((time.perf_counter() - started) * 1000),
        }
    except ObjectStorageNotConfigured:
        dependencies["objectStorage"] = {"status": "not_configured", "latencyMs": None}
    except Exception:
        dependencies["objectStorage"] = {"status": "error", "latencyMs": None}
    started = time.perf_counter()
    try:
        if not settings.casdoor_jwks_url:
            raise ValueError
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(str(settings.casdoor_jwks_url))
            response.raise_for_status()
        dependencies["casdoor"] = {
            "status": "ok",
            "latencyMs": round((time.perf_counter() - started) * 1000),
        }
    except Exception:
        dependencies["casdoor"] = {"status": "error", "latencyMs": None}
    data["service"] = {
        "name": "moment-one-server",
        "environment": settings.env,
        "version": settings.build_version,
        "commit": settings.build_commit,
        "buildTime": settings.build_time,
    }
    data["dependencies"] = dependencies
    return data


@router.get("/users", response_model=PageResponse)
async def list_users(
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminRepository = Depends(_admin_repo),
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = None,
    query: str | None = Query(default=None, max_length=120),
    status: str | None = Query(default=None, pattern="^(active|disabled)$"),
) -> PageResponse:
    admin.require("admin.users")
    items, has_more, next_cursor = await repo.list_users(
        limit=limit, cursor=cursor, query=query, status=status
    )
    return PageResponse(
        items=[_user_dict(item) for item in items], hasMore=has_more, nextCursor=next_cursor
    )


@router.patch("/users/{user_id}/status")
async def set_user_status(
    user_id: UUID,
    body: UserStatusRequest,
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminRepository = Depends(_admin_repo),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    admin.require("admin.users")
    if user_id == admin.user_id and body.status == "disabled":
        raise ApplicationError(
            code="INVALID_REQUEST", message="不能暂停当前管理员账号。", status_code=400
        )
    payload = {"userId": str(user_id), **body.model_dump()}
    idem, record = await _idempotency(
        session=session,
        admin=admin,
        operation="admin_set_user_status",
        key=idempotency_key,
        payload=payload,
    )
    if record.state == "completed" and record.response_body:
        return record.response_body
    user = await repo.set_user_status(
        user_id=user_id,
        status=body.status,
        expected_revision=body.expectedRevision,
        reason=body.reason,
    )
    if user is None:
        actual = await repo.user_revision(user_id)
        raise ApplicationError(
            code="REVISION_CONFLICT" if actual else "USER_NOT_FOUND",
            message="用户不存在或版本已变化。",
            status_code=409 if actual else 404,
            details={"actualRevision": actual},
        )
    response = _user_dict(user)
    await _audit(
        session,
        admin,
        event_type=f"admin.user.{body.status}",
        resource_type="user",
        resource_id=user_id,
        allowed=True,
        metadata={"reason": body.reason},
    )
    await idem.complete(
        record_id=record.id, response_status=200, response_body=response, resource_id=user_id
    )
    return response


@router.get("/device-bindings", response_model=PageResponse)
async def list_bindings(
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminRepository = Depends(_admin_repo),
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = None,
    status: str | None = Query(default=None, pattern="^(active|revoked)$"),
) -> PageResponse:
    admin.require("admin.security")
    items, more, next_cursor = await repo.list_bindings(limit=limit, cursor=cursor, status=status)
    return PageResponse(
        items=[_binding_dict(item) for item in items], hasMore=more, nextCursor=next_cursor
    )


@router.post("/device-bindings/{binding_id}/revoke")
async def revoke_binding(
    binding_id: UUID,
    body: RevokeRequest,
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminRepository = Depends(_admin_repo),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    admin.require("admin.security")
    payload = {"bindingId": str(binding_id), **body.model_dump()}
    idem, record = await _idempotency(
        session=session,
        admin=admin,
        operation="admin_revoke_binding",
        key=idempotency_key,
        payload=payload,
    )
    if record.state == "completed" and record.response_body:
        return record.response_body
    item = await repo.revoke_binding(binding_id=binding_id, expected_revision=body.expectedRevision)
    if item is None:
        actual = await repo.binding_revision(binding_id)
        raise ApplicationError(
            code="REVISION_CONFLICT" if actual else "BINDING_NOT_FOUND",
            message="设备绑定不存在或版本已变化。",
            status_code=409 if actual else 404,
            details={"actualRevision": actual},
        )
    response = _binding_dict(item)
    await _audit(
        session,
        admin,
        event_type="admin.device_binding.revoked",
        resource_type="device_binding",
        resource_id=binding_id,
        allowed=True,
        metadata={"reason": body.reason, "deviceId": item.device_id},
    )
    await idem.complete(
        record_id=record.id, response_status=200, response_body=response, resource_id=binding_id
    )
    return response


@router.get("/mcp-authorizations", response_model=PageResponse)
async def list_authorizations(
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminRepository = Depends(_admin_repo),
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = None,
    status: str | None = Query(default=None, pattern="^(active|revoked)$"),
) -> PageResponse:
    admin.require("admin.security")
    items, more, next_cursor = await repo.list_authorizations(
        limit=limit, cursor=cursor, status=status
    )
    return PageResponse(
        items=[_authorization_dict(item) for item in items], hasMore=more, nextCursor=next_cursor
    )


@router.post("/mcp-authorizations/{authorization_id}/revoke")
async def revoke_authorization(
    authorization_id: UUID,
    body: RevokeRequest,
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminRepository = Depends(_admin_repo),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    admin.require("admin.security")
    payload = {"authorizationId": str(authorization_id), **body.model_dump()}
    idem, record = await _idempotency(
        session=session,
        admin=admin,
        operation="admin_revoke_mcp_authorization",
        key=idempotency_key,
        payload=payload,
    )
    if record.state == "completed" and record.response_body:
        return record.response_body
    item = await repo.revoke_authorization(
        authorization_id=authorization_id, expected_revision=body.expectedRevision
    )
    if item is None:
        actual = await repo.authorization_revision(authorization_id)
        raise ApplicationError(
            code="REVISION_CONFLICT" if actual else "AUTHORIZATION_NOT_FOUND",
            message="MCP 授权不存在或版本已变化。",
            status_code=409 if actual else 404,
            details={"actualRevision": actual},
        )
    response = _authorization_dict(item)
    await _audit(
        session,
        admin,
        event_type="admin.mcp_authorization.revoked",
        resource_type="mcp_authorization",
        resource_id=authorization_id,
        allowed=True,
        metadata={"reason": body.reason, "clientId": item.client_id},
    )
    await idem.complete(
        record_id=record.id,
        response_status=200,
        response_body=response,
        resource_id=authorization_id,
    )
    return response


@router.get("/plans")
async def list_plans(
    admin: AdminContext = Depends(get_admin_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    admin.require("admin.read")
    from app.modules.entitlements.repository import EntitlementRepository

    plans = await EntitlementRepository(session).list_plans(active_only=False)
    return {
        "items": [
            {
                "key": item.key,
                "version": item.version,
                "name": item.name,
                "status": item.status,
                "entitlements": item.entitlements,
                "quotas": item.quotas,
                "updatedAt": item.updated_at.isoformat(),
            }
            for item in plans
        ]
    }


@router.get("/storage/summary")
async def storage_summary(
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminEntitlementRepository = Depends(_admin_entitlement_repo),
) -> dict[str, object]:
    admin.require("admin.read")
    return await repo.summary()


@router.get("/storage/accounts")
async def list_storage_accounts(
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminEntitlementRepository = Depends(_admin_entitlement_repo),
    query: str | None = Query(default=None, max_length=120),
    overQuota: bool | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, object]:
    admin.require("admin.read")
    items = await repo.list_accounts(query=query, over_quota=overQuota, limit=limit)
    return {"items": items, "hasMore": False, "nextCursor": None}


@router.get("/users/{user_id}/entitlements")
async def user_entitlement_detail(
    user_id: UUID,
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminEntitlementRepository = Depends(_admin_entitlement_repo),
) -> dict[str, object]:
    admin.require("admin.read")
    detail = await repo.account_detail(user_id)
    if detail is None:
        raise ApplicationError(code="USER_NOT_FOUND", message="用户不存在。", status_code=404)
    return detail


@router.patch("/users/{user_id}/plan")
async def set_user_plan(
    user_id: UUID,
    body: SetPlanRequest,
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminEntitlementRepository = Depends(_admin_entitlement_repo),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    admin.require("admin.operations")
    payload = {"userId": str(user_id), **body.model_dump(mode="json")}
    idem, record = await _idempotency(
        session=session,
        admin=admin,
        operation="admin_set_user_plan",
        key=idempotency_key,
        payload=payload,
    )
    if record.state == "completed" and record.response_body:
        return record.response_body
    detail = await repo.set_plan(
        user_id, plan_key=body.planKey, expected_revision=body.expectedRevision
    )
    if detail is None:
        actual = await repo.account_revision(user_id)
        raise ApplicationError(
            code="REVISION_CONFLICT" if actual else "USER_NOT_FOUND",
            message="用户存储账户不存在或版本已变化。",
            status_code=409 if actual else 404,
            details={"actualRevision": actual},
        )
    await _audit(
        session,
        admin,
        event_type="admin.user_plan.changed",
        resource_type="user",
        resource_id=user_id,
        allowed=True,
        metadata={"planKey": body.planKey, "reason": body.reason},
    )
    await idem.complete(
        record_id=record.id, response_status=200, response_body=detail, resource_id=user_id
    )
    return detail


@router.post("/users/{user_id}/storage-grants")
async def add_user_storage_grant(
    user_id: UUID,
    body: AddStorageGrantRequest,
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminEntitlementRepository = Depends(_admin_entitlement_repo),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    admin.require("admin.operations")
    if body.expiresAt is not None and body.expiresAt <= datetime.now(UTC):
        raise ApplicationError(
            code="INVALID_REQUEST", message="额度有效期必须晚于当前时间。", status_code=400
        )
    payload = {"userId": str(user_id), **body.model_dump(mode="json")}
    idem, record = await _idempotency(
        session=session,
        admin=admin,
        operation="admin_add_storage_grant",
        key=idempotency_key,
        payload=payload,
    )
    if record.state == "completed" and record.response_body:
        return record.response_body
    detail = await repo.add_storage_grant(
        user_id,
        quota_bytes=body.quotaBytes,
        expires_at=body.expiresAt,
        expected_revision=body.expectedRevision,
    )
    if detail is None:
        actual = await repo.account_revision(user_id)
        raise ApplicationError(
            code="REVISION_CONFLICT" if actual else "USER_NOT_FOUND",
            message="用户存储账户不存在或版本已变化。",
            status_code=409 if actual else 404,
            details={"actualRevision": actual},
        )
    await _audit(
        session,
        admin,
        event_type="admin.storage_grant.created",
        resource_type="user",
        resource_id=user_id,
        allowed=True,
        metadata={
            "quotaBytes": body.quotaBytes,
            "expiresAt": body.expiresAt.isoformat() if body.expiresAt else None,
            "reason": body.reason,
        },
    )
    await idem.complete(
        record_id=record.id, response_status=200, response_body=detail, resource_id=user_id
    )
    return detail


@router.post("/storage-grants/{grant_id}/revoke")
async def revoke_storage_grant(
    grant_id: UUID,
    body: RevokeRequest,
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminEntitlementRepository = Depends(_admin_entitlement_repo),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    admin.require("admin.operations")
    payload = {"grantId": str(grant_id), **body.model_dump(mode="json")}
    idem, record = await _idempotency(
        session=session,
        admin=admin,
        operation="admin_revoke_storage_grant",
        key=idempotency_key,
        payload=payload,
    )
    if record.state == "completed" and record.response_body:
        return record.response_body
    detail = await repo.revoke_storage_grant(grant_id, expected_revision=body.expectedRevision)
    if detail is None:
        actual = await repo.grant_revision(grant_id)
        raise ApplicationError(
            code="REVISION_CONFLICT" if actual else "STORAGE_GRANT_NOT_FOUND",
            message="存储额度不存在、已失效或版本已变化。",
            status_code=409 if actual else 404,
            details={"actualRevision": actual},
        )
    await _audit(
        session,
        admin,
        event_type="admin.storage_grant.revoked",
        resource_type="storage_quota_grant",
        resource_id=grant_id,
        allowed=True,
        metadata={"reason": body.reason},
    )
    await idem.complete(
        record_id=record.id, response_status=200, response_body=detail, resource_id=grant_id
    )
    return detail


@router.post("/users/{user_id}/storage/reconcile")
async def reconcile_user_storage(
    user_id: UUID,
    body: ReconcileStorageRequest,
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminEntitlementRepository = Depends(_admin_entitlement_repo),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    admin.require("admin.operations")
    payload = {"userId": str(user_id), **body.model_dump(mode="json")}
    idem, record = await _idempotency(
        session=session,
        admin=admin,
        operation="admin_reconcile_user_storage",
        key=idempotency_key,
        payload=payload,
    )
    if record.state == "completed" and record.response_body:
        return record.response_body
    detail = await repo.reconcile(user_id, expected_revision=body.expectedRevision)
    if detail is None:
        actual = await repo.account_revision(user_id)
        raise ApplicationError(
            code="REVISION_CONFLICT" if actual else "USER_NOT_FOUND",
            message="用户存储账户不存在或版本已变化。",
            status_code=409 if actual else 404,
            details={"actualRevision": actual},
        )
    await _audit(
        session,
        admin,
        event_type="admin.storage.reconciled",
        resource_type="user",
        resource_id=user_id,
        allowed=True,
        metadata={"reason": body.reason},
    )
    await idem.complete(
        record_id=record.id, response_status=200, response_body=detail, resource_id=user_id
    )
    return detail


@router.get("/audit-events", response_model=PageResponse)
async def list_audit_events(
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminRepository = Depends(_admin_repo),
    limit: int = Query(default=40, ge=1, le=100),
    cursor: str | None = None,
    eventType: str | None = Query(default=None, max_length=120),
    allowed: bool | None = None,
    actorType: str | None = Query(default=None, max_length=24),
) -> PageResponse:
    admin.require("admin.security")
    items, more, next_cursor = await repo.list_audit_events(
        limit=limit, cursor=cursor, event_type=eventType, allowed=allowed, actor_type=actorType
    )
    return PageResponse(
        items=[_audit_dict(item) for item in items], hasMore=more, nextCursor=next_cursor
    )


@router.post("/maintenance/expired-records/preview")
async def preview_expired_records(
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminRepository = Depends(_admin_repo),
) -> dict[str, object]:
    admin.require("admin.operations")
    return {
        "previewedAt": datetime.now(UTC).isoformat(),
        "counts": await repo.expired_record_counts(),
    }


@router.post("/maintenance/expired-records/confirm")
async def confirm_expired_records(
    body: MaintenanceConfirmRequest,
    admin: AdminContext = Depends(get_admin_context),
    repo: AdminRepository = Depends(_admin_repo),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    admin.require("admin.operations")
    if (datetime.now(UTC) - body.previewedAt).total_seconds() > 300:
        raise ApplicationError(
            code="CONFIRMATION_EXPIRED", message="维护预览已过期，请重新预览。", status_code=409
        )
    payload = body.model_dump(mode="json")
    idem, record = await _idempotency(
        session=session,
        admin=admin,
        operation="admin_expire_records",
        key=idempotency_key,
        payload=payload,
    )
    if record.state == "completed" and record.response_body:
        return record.response_body
    counts = await repo.expire_records()
    response: dict[str, object] = {"completedAt": datetime.now(UTC).isoformat(), "counts": counts}
    await _audit(
        session,
        admin,
        event_type="admin.maintenance.expired_records",
        resource_type="maintenance",
        resource_id=None,
        allowed=True,
        metadata=counts,
    )
    await idem.complete(record_id=record.id, response_status=200, response_body=response)
    return response
