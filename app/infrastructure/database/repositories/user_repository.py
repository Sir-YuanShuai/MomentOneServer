from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import User as UserORM
from app.infrastructure.identity.casdoor import AuthenticatedPrincipal, CasdoorTokenVerifier


class UserRepository:
    """本地用户表读写，首次登录时从 Casdoor 同步。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_casdoor_sub(self, casdoor_sub: str) -> UserORM | None:
        stmt = select(UserORM).where(UserORM.casdoor_sub == casdoor_sub)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_from_casdoor(
        self,
        principal: AuthenticatedPrincipal,
        casdoor_user_id: str,
    ) -> UUID:
        """查找或创建本地用户记录，返回本地 UUID。"""
        existing = await self.get_by_casdoor_sub(principal.subject)
        if existing:
            # 更新 display_name / email（可能用户在 Casdoor 改了）
            if principal.display_name and principal.display_name != existing.display_name:
                existing.display_name = principal.display_name
            if principal.email and principal.email != existing.email:
                existing.email = principal.email
            await self._session.flush()
            return existing.id

        new_user = UserORM(
            casdoor_sub=principal.subject,
            casdoor_user_id=casdoor_user_id,
            display_name=principal.display_name,
            email=principal.email,
        )
        self._session.add(new_user)
        await self._session.flush()
        return new_user.id


async def resolve_user_id(
    session: AsyncSession,
    verifier: CasdoorTokenVerifier,
    access_token: str,
) -> UUID:
    """完整认证流程：验签 → 查本地 users → 不存在则调 userinfo 同步。

    Returns:
        本地 users 表中的 UUID 主键。
    """
    principal = verifier.verify(access_token)
    user_repo = UserRepository(session)
    existing = await user_repo.get_by_casdoor_sub(principal.subject)

    if existing:
        return existing.id

    # 首次登录：调 Casdoor userinfo 拿 UUID
    userinfo = await verifier.fetch_userinfo(access_token)
    casdoor_user_id = userinfo.get("id") or userinfo.get("sub") or principal.subject

    return await user_repo.upsert_from_casdoor(principal, str(casdoor_user_id))
