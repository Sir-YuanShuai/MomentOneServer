from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import (
    Asset,
    AuditEvent,
    BindingCode,
    Device,
    DeviceBinding,
    McpAuthorization,
    McpOAuthCode,
    Moment,
    PendingConfirmation,
    User,
)
from app.modules.admin.domain import (
    AdminAuditEvent,
    AdminAuthorization,
    AdminBinding,
    AdminUser,
)


def _cursor_filter(model, cursor: str | None, time_column):
    if not cursor:
        return None
    try:
        raw_time, raw_id = cursor.rsplit("|", 1)
        cursor_time = datetime.fromisoformat(raw_time)
        cursor_id = UUID(raw_id)
    except (ValueError, TypeError):
        return None
    return or_(time_column < cursor_time, and_(time_column == cursor_time, model.id < cursor_id))


class AdminRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def overview(self) -> dict[str, object]:
        now = datetime.now(UTC)
        since = now - timedelta(hours=24)

        async def count(stmt) -> int:
            return int((await self._session.scalar(stmt)) or 0)

        asset_states_result = await self._session.execute(
            select(Asset.state, func.count(Asset.id)).group_by(Asset.state)
        )
        asset_states = {str(state): int(total) for state, total in asset_states_result.all()}
        return {
            "generatedAt": now.isoformat(),
            "users": {
                "total": await count(select(func.count(User.id))),
                "active": await count(select(func.count(User.id)).where(User.status == "active")),
                "disabled": await count(
                    select(func.count(User.id)).where(User.status == "disabled")
                ),
                "new24h": await count(select(func.count(User.id)).where(User.created_at >= since)),
            },
            "moments": {
                "total": await count(
                    select(func.count(Moment.id)).where(Moment.deleted_at.is_(None))
                ),
                "new24h": await count(
                    select(func.count(Moment.id)).where(
                        Moment.deleted_at.is_(None), Moment.created_at >= since
                    )
                ),
            },
            "assets": {"total": sum(asset_states.values()), "byState": asset_states},
            "access": {
                "activeDeviceBindings": await count(
                    select(func.count(DeviceBinding.id)).where(DeviceBinding.status == "active")
                ),
                "activeMcpAuthorizations": await count(
                    select(func.count(McpAuthorization.id)).where(
                        McpAuthorization.status == "active"
                    )
                ),
            },
            "security": {
                "denied24h": await count(
                    select(func.count(AuditEvent.id)).where(
                        AuditEvent.allowed.is_(False), AuditEvent.created_at >= since
                    )
                )
            },
            "maintenance": await self.expired_record_counts(),
        }

    async def list_users(
        self, *, limit: int, cursor: str | None, query: str | None, status: str | None
    ) -> tuple[list[AdminUser], bool, str | None]:
        moment_count = (
            select(func.count(Moment.id))
            .where(Moment.user_id == User.id, Moment.deleted_at.is_(None))
            .correlate(User)
            .scalar_subquery()
        )
        binding_count = (
            select(func.count(DeviceBinding.id))
            .where(DeviceBinding.user_id == User.id, DeviceBinding.status == "active")
            .correlate(User)
            .scalar_subquery()
        )
        mcp_count = (
            select(func.count(McpAuthorization.id))
            .where(McpAuthorization.user_id == User.id, McpAuthorization.status == "active")
            .correlate(User)
            .scalar_subquery()
        )
        stmt = select(User, moment_count, binding_count, mcp_count).order_by(
            User.created_at.desc(), User.id.desc()
        )
        if status:
            stmt = stmt.where(User.status == status)
        if query:
            pattern = f"%{query.strip()}%"
            stmt = stmt.where(
                or_(
                    User.display_name.ilike(pattern),
                    User.email.ilike(pattern),
                    User.casdoor_sub.ilike(pattern),
                )
            )
        condition = _cursor_filter(User, cursor, User.created_at)
        if condition is not None:
            stmt = stmt.where(condition)
        rows = (await self._session.execute(stmt.limit(limit + 1))).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            AdminUser(
                id=user.id,
                display_name=user.display_name,
                email=user.email,
                casdoor_sub=user.casdoor_sub,
                status=user.status,
                revision=user.revision,
                created_at=user.created_at,
                updated_at=user.updated_at,
                last_active_at=user.last_active_at,
                disabled_at=user.disabled_at,
                disable_reason=user.disable_reason,
                moment_count=int(moments or 0),
                active_binding_count=int(bindings or 0),
                active_mcp_count=int(authorizations or 0),
            )
            for user, moments, bindings, authorizations in rows
        ]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = f"{last.created_at.isoformat()}|{last.id}"
        return items, has_more, next_cursor

    async def set_user_status(
        self, *, user_id: UUID, status: str, expected_revision: int, reason: str | None
    ) -> AdminUser | None:
        result = await self._session.execute(
            select(User).where(User.id == user_id, User.revision == expected_revision)
        )
        user = result.scalar_one_or_none()
        if user is None:
            return None
        now = datetime.now(UTC)
        user.status = status
        user.revision += 1
        user.disabled_at = now if status == "disabled" else None
        user.disable_reason = reason if status == "disabled" else None
        user.updated_at = now
        await self._session.flush()
        return AdminUser(
            id=user.id,
            display_name=user.display_name,
            email=user.email,
            casdoor_sub=user.casdoor_sub,
            status=user.status,
            revision=user.revision,
            created_at=user.created_at,
            updated_at=user.updated_at,
            last_active_at=user.last_active_at,
            disabled_at=user.disabled_at,
            disable_reason=user.disable_reason,
            moment_count=0,
            active_binding_count=0,
            active_mcp_count=0,
        )

    async def user_revision(self, user_id: UUID) -> int | None:
        return await self._session.scalar(select(User.revision).where(User.id == user_id))

    async def list_bindings(
        self, *, limit: int, cursor: str | None, status: str | None
    ) -> tuple[list[AdminBinding], bool, str | None]:
        stmt = (
            select(DeviceBinding, Device, User)
            .join(Device, Device.id == DeviceBinding.device_id)
            .join(User, User.id == DeviceBinding.user_id)
            .order_by(DeviceBinding.bound_at.desc(), DeviceBinding.id.desc())
        )
        if status:
            stmt = stmt.where(DeviceBinding.status == status)
        condition = _cursor_filter(DeviceBinding, cursor, DeviceBinding.bound_at)
        if condition is not None:
            stmt = stmt.where(condition)
        rows = (await self._session.execute(stmt.limit(limit + 1))).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            AdminBinding(
                id=binding.id,
                user_id=user.id,
                user_display_name=user.display_name,
                user_email=user.email,
                device_id=binding.device_id,
                device_name=device.device_name,
                scope=tuple(binding.scope or ()),
                status=binding.status,
                revision=binding.revision,
                bound_at=binding.bound_at,
                last_active_at=binding.last_active_at,
                revoked_at=binding.revoked_at,
            )
            for binding, device, user in rows
        ]
        next_cursor = (
            f"{items[-1].bound_at.isoformat()}|{items[-1].id}" if has_more and items else None
        )
        return items, has_more, next_cursor

    async def revoke_binding(
        self, *, binding_id: UUID, expected_revision: int
    ) -> AdminBinding | None:
        row = (
            await self._session.execute(
                select(DeviceBinding, Device, User)
                .join(Device, Device.id == DeviceBinding.device_id)
                .join(User, User.id == DeviceBinding.user_id)
                .where(DeviceBinding.id == binding_id, DeviceBinding.revision == expected_revision)
            )
        ).one_or_none()
        if row is None:
            return None
        binding, device, user = row
        now = datetime.now(UTC)
        binding.status = "revoked"
        binding.revoked_at = now
        binding.refresh_token_hash = None
        binding.revision += 1
        authorization = (
            await self._session.execute(
                select(McpAuthorization).where(
                    McpAuthorization.user_id == binding.user_id,
                    McpAuthorization.client_id == f"glasses:{binding.device_id}",
                )
            )
        ).scalar_one_or_none()
        if authorization is not None and authorization.status == "active":
            authorization.status = "revoked"
            authorization.revoked_at = now
            authorization.updated_at = now
            authorization.revision += 1
        await self._session.flush()
        return AdminBinding(
            id=binding.id,
            user_id=user.id,
            user_display_name=user.display_name,
            user_email=user.email,
            device_id=binding.device_id,
            device_name=device.device_name,
            scope=tuple(binding.scope or ()),
            status=binding.status,
            revision=binding.revision,
            bound_at=binding.bound_at,
            last_active_at=binding.last_active_at,
            revoked_at=binding.revoked_at,
        )

    async def binding_revision(self, binding_id: UUID) -> int | None:
        return await self._session.scalar(
            select(DeviceBinding.revision).where(DeviceBinding.id == binding_id)
        )

    async def list_authorizations(
        self, *, limit: int, cursor: str | None, status: str | None
    ) -> tuple[list[AdminAuthorization], bool, str | None]:
        stmt = (
            select(McpAuthorization, User)
            .join(User, User.id == McpAuthorization.user_id)
            .order_by(McpAuthorization.created_at.desc(), McpAuthorization.id.desc())
        )
        if status:
            stmt = stmt.where(McpAuthorization.status == status)
        condition = _cursor_filter(McpAuthorization, cursor, McpAuthorization.created_at)
        if condition is not None:
            stmt = stmt.where(condition)
        rows = (await self._session.execute(stmt.limit(limit + 1))).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            AdminAuthorization(
                id=authorization.id,
                user_id=user.id,
                user_display_name=user.display_name,
                user_email=user.email,
                client_id=authorization.client_id,
                client_name=authorization.client_name,
                client_type=authorization.client_type,
                scope=tuple(authorization.scope.split()),
                status=authorization.status,
                revision=authorization.revision,
                created_at=authorization.created_at,
                updated_at=authorization.updated_at,
                last_active_at=authorization.last_active_at,
                revoked_at=authorization.revoked_at,
            )
            for authorization, user in rows
        ]
        next_cursor = (
            f"{items[-1].created_at.isoformat()}|{items[-1].id}" if has_more and items else None
        )
        return items, has_more, next_cursor

    async def revoke_authorization(
        self, *, authorization_id: UUID, expected_revision: int
    ) -> AdminAuthorization | None:
        row = (
            await self._session.execute(
                select(McpAuthorization, User)
                .join(User, User.id == McpAuthorization.user_id)
                .where(
                    McpAuthorization.id == authorization_id,
                    McpAuthorization.revision == expected_revision,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        authorization, user = row
        now = datetime.now(UTC)
        authorization.status = "revoked"
        authorization.revoked_at = now
        authorization.updated_at = now
        authorization.revision += 1
        await self._session.flush()
        return AdminAuthorization(
            id=authorization.id,
            user_id=user.id,
            user_display_name=user.display_name,
            user_email=user.email,
            client_id=authorization.client_id,
            client_name=authorization.client_name,
            client_type=authorization.client_type,
            scope=tuple(authorization.scope.split()),
            status=authorization.status,
            revision=authorization.revision,
            created_at=authorization.created_at,
            updated_at=authorization.updated_at,
            last_active_at=authorization.last_active_at,
            revoked_at=authorization.revoked_at,
        )

    async def authorization_revision(self, authorization_id: UUID) -> int | None:
        return await self._session.scalar(
            select(McpAuthorization.revision).where(McpAuthorization.id == authorization_id)
        )

    async def list_audit_events(
        self,
        *,
        limit: int,
        cursor: str | None,
        event_type: str | None,
        allowed: bool | None,
        actor_type: str | None,
    ) -> tuple[list[AdminAuditEvent], bool, str | None]:
        stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        if event_type:
            stmt = stmt.where(AuditEvent.event_type == event_type)
        if allowed is not None:
            stmt = stmt.where(AuditEvent.allowed == allowed)
        if actor_type:
            stmt = stmt.where(AuditEvent.actor_type == actor_type)
        condition = _cursor_filter(AuditEvent, cursor, AuditEvent.created_at)
        if condition is not None:
            stmt = stmt.where(condition)
        rows = list((await self._session.execute(stmt.limit(limit + 1))).scalars().all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            AdminAuditEvent(
                id=row.id,
                user_id=row.user_id,
                actor_type=row.actor_type,
                actor_id=row.actor_id,
                event_type=row.event_type,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                request_id=row.request_id,
                allowed=row.allowed,
                reason=row.reason,
                metadata=dict(row.metadata_ or {}),
                created_at=row.created_at,
            )
            for row in rows
        ]
        next_cursor = (
            f"{items[-1].created_at.isoformat()}|{items[-1].id}" if has_more and items else None
        )
        return items, has_more, next_cursor

    async def expired_record_counts(self) -> dict[str, int]:
        now = datetime.now(UTC)

        async def count(stmt) -> int:
            return int((await self._session.scalar(stmt)) or 0)

        return {
            "bindingCodes": await count(
                select(func.count(BindingCode.id)).where(
                    BindingCode.status == "pending", BindingCode.expires_at < now
                )
            ),
            "oauthCodes": await count(
                select(func.count(McpOAuthCode.id)).where(
                    McpOAuthCode.status == "pending", McpOAuthCode.expires_at < now
                )
            ),
            "confirmations": await count(
                select(func.count(PendingConfirmation.id)).where(
                    PendingConfirmation.status == "pending", PendingConfirmation.expires_at < now
                )
            ),
        }

    async def expire_records(self) -> dict[str, int]:
        now = datetime.now(UTC)
        binding = await self._session.execute(
            update(BindingCode)
            .where(BindingCode.status == "pending", BindingCode.expires_at < now)
            .values(status="expired")
        )
        oauth = await self._session.execute(
            update(McpOAuthCode)
            .where(McpOAuthCode.status == "pending", McpOAuthCode.expires_at < now)
            .values(status="expired")
        )
        confirmations = await self._session.execute(
            update(PendingConfirmation)
            .where(PendingConfirmation.status == "pending", PendingConfirmation.expires_at < now)
            .values(status="expired")
        )
        await self._session.flush()
        return {
            "bindingCodes": int(getattr(binding, "rowcount", 0) or 0),
            "oauthCodes": int(getattr(oauth, "rowcount", 0) or 0),
            "confirmations": int(getattr(confirmations, "rowcount", 0) or 0),
        }
