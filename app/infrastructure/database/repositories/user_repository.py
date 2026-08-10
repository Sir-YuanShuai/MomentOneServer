from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApplicationError
from app.infrastructure.database.models import User as UserORM
from app.infrastructure.identity.casdoor import AuthenticatedPrincipal, CasdoorTokenVerifier
from app.modules.accounts.repository import IdentityRepository


class UserRepository:
    """内部 User 与外部 Identity 映射；旧 casdoor_sub 仅作为兼容回退。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._identities = IdentityRepository(session)

    async def get_by_casdoor_sub(self, casdoor_sub: str) -> UserORM | None:
        result = await self._session.execute(
            select(UserORM).where(UserORM.casdoor_sub == casdoor_sub)
        )
        return result.scalar_one_or_none()

    async def get_by_identity(self, issuer: str, subject: str) -> UserORM | None:
        identity = await self._identities.get_by_external(issuer, subject)
        if identity is None:
            return None
        return await self.get(identity.user_id)

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

    async def sync_profile(
        self,
        user: UserORM,
        principal: AuthenticatedPrincipal,
        *,
        fill_missing_only: bool = True,
    ) -> UserORM:
        def assign(field: str, value: object) -> None:
            if value is None or value == "":
                return
            if not fill_missing_only or getattr(user, field) in (None, ""):
                setattr(user, field, value)

        assign("display_name", principal.display_name)
        assign("email", principal.email)
        assign("phone", principal.phone)
        assign("avatar_url", principal.avatar_url)
        if principal.email_verified:
            user.email_verified = True
        if principal.phone_verified:
            user.phone_verified = True
        await self._session.flush()
        return user

    async def ensure_identity(
        self,
        user: UserORM,
        principal: AuthenticatedPrincipal,
    ) -> None:
        await self._identities.ensure_oidc(
            user_id=user.id,
            issuer=principal.issuer.rstrip("/"),
            subject=principal.subject,
            identifier=principal.email or principal.phone or principal.username,
            display_name=principal.display_name,
            metadata={
                "owner": principal.owner,
                "username": principal.username,
                "email": principal.email,
                "phone": principal.phone,
            },
        )

    async def upsert_from_casdoor(
        self, principal: AuthenticatedPrincipal, casdoor_user_id: str
    ) -> UserORM:
        issuer = principal.issuer.rstrip("/")
        existing = await self.get_by_identity(issuer, principal.subject)
        if existing is None:
            # 兼容旧数据：首次命中 casdoor_sub 时自动回填 user_identities。
            existing = await self.get_by_casdoor_sub(principal.subject)
        if existing:
            await self.ensure_identity(existing, principal)
            return await self.sync_profile(existing, principal)
        new_user = UserORM(
            casdoor_sub=principal.subject,
            casdoor_user_id=casdoor_user_id,
            display_name=principal.display_name,
            email=principal.email,
            phone=principal.phone,
            email_verified=principal.email_verified,
            phone_verified=principal.phone_verified,
            avatar_url=principal.avatar_url,
            status="active",
            revision=1,
        )
        self._session.add(new_user)
        await self._session.flush()
        await self.ensure_identity(new_user, principal)
        from app.modules.entitlements.repository import EntitlementRepository

        await EntitlementRepository(self._session).ensure_user_defaults(new_user.id)
        return new_user


async def resolve_user_id(
    session: AsyncSession, verifier: CasdoorTokenVerifier, access_token: str
) -> UUID:
    principal = verifier.verify(access_token)
    userinfo: dict[str, object] | None = None
    if verifier.required_organization:
        userinfo = await verifier.fetch_account(access_token)
        principal = principal.merge_userinfo(userinfo)
        verifier.ensure_organization(principal)
    user_repo = UserRepository(session)
    issuer = principal.issuer.rstrip("/")
    user = await user_repo.get_by_identity(issuer, principal.subject)
    if user is None:
        if userinfo is None:
            userinfo = await verifier.fetch_userinfo(access_token)
        merged = principal.merge_userinfo(userinfo)
        casdoor_user_id = userinfo.get("id") or userinfo.get("sub") or principal.subject
        user = await user_repo.upsert_from_casdoor(merged, str(casdoor_user_id))
    else:
        await user_repo.ensure_identity(user, principal)
        user = await user_repo.sync_profile(user, principal)
    UserRepository.ensure_active(user)
    await user_repo.touch_active(user)
    return user.id
