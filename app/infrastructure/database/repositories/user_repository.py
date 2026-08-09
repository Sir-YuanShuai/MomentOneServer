from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApplicationError
from app.infrastructure.database.models import User as UserORM
from app.infrastructure.identity.casdoor import AuthenticatedPrincipal, CasdoorTokenVerifier


class UserRepository:
    """本地用户表读写，首次登录时从 Casdoor 同步。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_casdoor_sub(self, casdoor_sub: str) -> UserORM | None:
        result = await self._session.execute(
            select(UserORM).where(UserORM.casdoor_sub == casdoor_sub)
        )
        return result.scalar_one_or_none()

    async def get(self, user_id: UUID) -> UserORM | None:
        result = await self._session.execute(select(UserORM).where(UserORM.id == user_id))
        return result.scalar_one_or_none()

    async def touch_active(self, user: UserORM) -> None:
        now = datetime.now(UTC)
        if user.last_active_at is None or now - user.last_active_at > timedelta(minutes=5):
            user.last_active_at = now
            await self._session.flush()

    @staticmethod
    def ensure_active(user: UserORM) -> None:
        if user.status != "active":
            raise ApplicationError(
                code="ACCOUNT_SUSPENDED",
                message="当前 Moment One 账号已被暂停。",
                status_code=403,
            )

    async def upsert_from_casdoor(
        self, principal: AuthenticatedPrincipal, casdoor_user_id: str
    ) -> UserORM:
        existing = await self.get_by_casdoor_sub(principal.subject)
        if existing:
            if principal.display_name and principal.display_name != existing.display_name:
                existing.display_name = principal.display_name
            if principal.email and principal.email != existing.email:
                existing.email = principal.email
            await self._session.flush()
            return existing
        new_user = UserORM(
            casdoor_sub=principal.subject,
            casdoor_user_id=casdoor_user_id,
            display_name=principal.display_name,
            email=principal.email,
            status="active",
            revision=1,
        )
        self._session.add(new_user)
        await self._session.flush()
        from app.modules.entitlements.repository import EntitlementRepository

        await EntitlementRepository(self._session).ensure_user_defaults(new_user.id)
        return new_user


async def resolve_user_id(
    session: AsyncSession, verifier: CasdoorTokenVerifier, access_token: str
) -> UUID:
    principal = verifier.verify(access_token)
    user_repo = UserRepository(session)
    user = await user_repo.get_by_casdoor_sub(principal.subject)
    if user is None:
        userinfo = await verifier.fetch_userinfo(access_token)
        casdoor_user_id = userinfo.get("id") or userinfo.get("sub") or principal.subject
        user = await user_repo.upsert_from_casdoor(
            principal.merge_userinfo(userinfo), str(casdoor_user_id)
        )
    UserRepository.ensure_active(user)
    await user_repo.touch_active(user)
    return user.id
