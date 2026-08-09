from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApplicationError
from app.infrastructure.database.models import (
    Asset,
    AuditEvent,
    DeviceBinding,
    HabitGoal,
    McpAuthorization,
    McpOAuthCode,
    Moment,
    MomentAsset,
    MomentRevision,
    User,
)
from app.infrastructure.database.repositories.confirmation_repository import (
    SqlConfirmationRepository,
)
from app.infrastructure.storage.object_storage import ObjectStorage

CONFIRMATION_PHRASE = "永久注销"


class AccountDeletionService:
    def __init__(self, session: AsyncSession, storage: ObjectStorage | None) -> None:
        self._session = session
        self._storage = storage
        self._confirmations = SqlConfirmationRepository(session)

    async def preview(self, user_id: UUID) -> dict[str, object]:
        user = await self._session.scalar(select(User).where(User.id == user_id))
        if user is None:
            raise ApplicationError(code="USER_NOT_FOUND", message="用户不存在。", status_code=404)

        async def count(model, condition) -> int:
            return int(
                await self._session.scalar(select(func.count()).select_from(model).where(condition))
                or 0
            )

        counts = {
            "moments": await count(Moment, Moment.user_id == user_id),
            "assets": await count(Asset, Asset.user_id == user_id),
            "habitGoals": await count(HabitGoal, HabitGoal.user_id == user_id),
            "deviceBindings": await count(DeviceBinding, DeviceBinding.user_id == user_id),
            "mcpAuthorizations": await count(McpAuthorization, McpAuthorization.user_id == user_id),
        }
        confirmation = await self._confirmations.create(
            user_id=user_id,
            target_type="account",
            target_id=user_id,
            action="delete_account",
            expected_revision=user.revision,
            preview={"counts": counts, "confirmationPhrase": CONFIRMATION_PHRASE},
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        return {
            "confirmationId": str(confirmation.id),
            "expiresAt": confirmation.expires_at.isoformat(),
            "confirmationPhrase": CONFIRMATION_PHRASE,
            "counts": counts,
            "warning": "注销会永久删除该账号在 Moment One 中的全部数据，且无法恢复。",
        }

    async def confirm(
        self,
        user_id: UUID,
        *,
        confirmation_id: UUID,
        confirmation_phrase: str,
        issued_at: int | None,
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        if (
            issued_at is None
            or now.timestamp() - issued_at > 300
            or issued_at > now.timestamp() + 30
        ):
            raise ApplicationError(
                code="REAUTHENTICATION_REQUIRED",
                message="注销账号前必须重新验证 Casdoor 登录密码。",
                status_code=401,
                details={"maxAgeSeconds": 300},
            )
        if confirmation_phrase.strip() != CONFIRMATION_PHRASE:
            raise ApplicationError(
                code="CONFIRMATION_PHRASE_INVALID",
                message=f"请输入“{CONFIRMATION_PHRASE}”确认注销。",
                status_code=400,
            )
        confirmation = await self._confirmations.get(confirmation_id)
        if (
            confirmation is None
            or confirmation.user_id != user_id
            or confirmation.target_id != user_id
            or confirmation.action != "delete_account"
        ):
            raise ApplicationError(
                code="CONFIRMATION_NOT_FOUND", message="注销确认不存在。", status_code=404
            )
        if confirmation.status != "pending" or confirmation.expires_at <= now:
            raise ApplicationError(
                code="CONFIRMATION_EXPIRED", message="注销确认已失效，请重新预览。", status_code=409
            )
        user = await self._session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            raise ApplicationError(code="USER_NOT_FOUND", message="用户不存在。", status_code=404)
        if user.revision != confirmation.expected_revision:
            raise ApplicationError(
                code="REVISION_CONFLICT",
                message="账号状态已经变化，请重新发起注销。",
                status_code=409,
                details={"actualRevision": user.revision},
            )

        assets = list(
            (await self._session.execute(select(Asset.id).where(Asset.user_id == user_id)))
            .scalars()
            .all()
        )
        if assets and self._storage is None:
            raise ApplicationError(
                code="SERVICE_UNAVAILABLE",
                message="对象存储暂不可用，不能安全完成账号注销。",
                status_code=503,
            )
        if self._storage is not None:
            for asset_id in assets:
                self._storage.delete_asset_objects(user_id=str(user_id), asset_id=str(asset_id))

        # 没有 users 外键的历史表显式清理；其余用户表由 users 的 ON DELETE CASCADE 清理。
        await self._session.execute(delete(MomentAsset).where(MomentAsset.user_id == user_id))
        await self._session.execute(delete(MomentRevision).where(MomentRevision.user_id == user_id))
        await self._session.execute(delete(Moment).where(Moment.user_id == user_id))
        await self._session.execute(delete(HabitGoal).where(HabitGoal.user_id == user_id))
        await self._session.execute(
            delete(AuditEvent).where(
                (AuditEvent.user_id == user_id) | (AuditEvent.actor_id == str(user_id))
            )
        )
        await self._session.execute(delete(McpOAuthCode).where(McpOAuthCode.user_id == user_id))
        await self._session.execute(delete(User).where(User.id == user_id))
        await self._session.flush()
        return {"deleted": True, "deletedAt": now.isoformat()}
