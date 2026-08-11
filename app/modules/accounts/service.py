from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlparse
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.infrastructure.database.models import (
    AccountLinkSession,
    ContactVerificationChallenge,
    User,
    UserIdentity,
)
from app.infrastructure.database.repositories.audit_event_repository import (
    SqlAuditEventRepository,
)
from app.infrastructure.database.repositories.confirmation_repository import (
    SqlConfirmationRepository,
)
from app.infrastructure.identity.casdoor import CasdoorTokenVerifier
from app.infrastructure.identity.casdoor_management import (
    CasdoorManagementClient,
    generate_pkce_verifier,
)
from app.modules.accounts.providers import (
    has_casdoor_provider_link,
    normalize_login_provider,
)
from app.modules.accounts.repository import IdentityRepository
from app.modules.admin.account import AccountRepository

UNLINK_PHRASE = "解除绑定"


class AccountCenterService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._casdoor = CasdoorManagementClient(settings)
        self._verifier = CasdoorTokenVerifier(settings)
        self._identities = IdentityRepository(session)
        self._confirmations = SqlConfirmationRepository(session)

    async def _user(self, user_id: UUID, *, lock: bool = False) -> User:
        stmt = select(User).where(User.id == user_id)
        if lock:
            stmt = stmt.with_for_update()
        user = await self._session.scalar(stmt)
        if user is None:
            raise ApplicationError(code="USER_NOT_FOUND", message="用户不存在。", status_code=404)
        return user

    async def _audit(
        self,
        user_id: UUID,
        *,
        event_type: str,
        resource_type: str,
        resource_id: UUID | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        await SqlAuditEventRepository(self._session).append(
            user_id=user_id,
            actor_type="web",
            actor_id=str(user_id),
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=None,
            allowed=True,
            metadata=metadata,
        )

    async def update_profile(
        self,
        user_id: UUID,
        *,
        locale: str,
        timezone: str | None,
        expected_revision: int,
    ) -> dict[str, object]:
        user = await self._user(user_id, lock=True)
        if user.revision != expected_revision:
            raise ApplicationError(
                code="REVISION_CONFLICT",
                message="账号资料已经变化，请刷新后重试。",
                status_code=409,
                details={"actualRevision": user.revision},
            )
        # displayName 等身份资料由浏览器使用用户 Token 直连 Casdoor；
        # 服务端这里只保存 Moment One 应用偏好（语言 / 时区）。账号快照读取时会从
        # Casdoor userinfo 同步最新展示资料，无需额外 sync 写接口。
        user.locale = locale
        user.timezone = timezone
        user.revision += 1
        user.profile_sync_status = "synced"
        user.profile_sync_error = None
        user.profile_synced_at = datetime.now(UTC)
        await self._session.flush()
        await self._audit(
            user_id,
            event_type="account.preferences.updated",
            resource_type="user",
            resource_id=user_id,
        )
        account = await AccountRepository(self._session).account(user_id)
        assert account is not None
        return account

    async def upload_avatar(
        self,
        user_id: UUID,
        *,
        filename: str,
        content_type: str,
        content: bytes,
        expected_revision: int,
        access_token: str | None = None,
    ) -> dict[str, object]:
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ApplicationError(
                code="AVATAR_TYPE_UNSUPPORTED",
                message="头像仅支持 JPEG、PNG 或 WebP。",
                status_code=415,
            )
        if len(content) > 5 * 1024 * 1024:
            raise ApplicationError(
                code="AVATAR_TOO_LARGE", message="头像不能超过 5 MiB。", status_code=413
            )
        user = await self._user(user_id, lock=True)
        if user.revision != expected_revision:
            raise ApplicationError(
                code="REVISION_CONFLICT",
                message="账号资料已经变化，请刷新后重试。",
                status_code=409,
                details={"actualRevision": user.revision},
            )
        avatar_url = await self._casdoor.upload_avatar(
            user.casdoor_user_id,
            filename=filename,
            content_type=content_type,
            content=content,
            access_token=access_token,
        )
        user.avatar_url = avatar_url
        user.profile_sync_status = "synced"
        user.profile_sync_error = None
        user.profile_synced_at = datetime.now(UTC)
        user.revision += 1
        await self._audit(
            user_id,
            event_type="account.avatar.updated",
            resource_type="user",
            resource_id=user_id,
        )
        account = await AccountRepository(self._session).account(user_id)
        assert account is not None
        return account

    async def unlink_login_provider(
        self,
        user_id: UUID,
        *,
        provider: str,
        access_token: str | None = None,
    ) -> None:
        normalized_provider = normalize_login_provider(provider)
        if normalized_provider is None:
            raise ApplicationError(
                code="IDENTITY_LINK_PROVIDER_UNSUPPORTED",
                message="暂不支持该登录方式。",
                status_code=400,
            )
        user = await self._user(user_id)
        await self._casdoor.unlink_provider(
            user.casdoor_user_id,
            provider=normalized_provider,
            access_token=access_token,
        )
        await self._audit(
            user_id,
            event_type="account.login_provider.unlinked",
            resource_type="identity_provider",
            metadata={"provider": normalized_provider},
        )

    async def start_contact_challenge(
        self,
        user_id: UUID,
        *,
        kind: str,
        destination: str,
        country_code: str | None,
        expected_revision: int,
        access_token: str | None = None,
    ) -> dict[str, object]:
        user = await self._user(user_id, lock=True)
        if user.revision != expected_revision:
            raise ApplicationError(
                code="REVISION_CONFLICT",
                message="账号资料已经变化，请刷新后重试。",
                status_code=409,
                details={"actualRevision": user.revision},
            )
        destination = self._normalize_contact(kind, destination)
        local = await self._identities.get_by_external(kind, destination, active_only=False)
        if local is not None and local.user_id != user_id:
            raise ApplicationError(
                code="IDENTITY_LINK_CONFLICT",
                message="该联系方式已经属于另一个 Moment One 账号。",
                status_code=409,
            )
        casdoor_user = await self._casdoor.get_user(user.casdoor_user_id, access_token=access_token)
        application = str(
            casdoor_user.get("signupApplication") or self._settings.casdoor_application or ""
        )
        organization = str(casdoor_user.get("owner") or self._settings.casdoor_organization or "")
        if not application or not organization:
            raise ApplicationError(
                code="IDENTITY_VERIFICATION_NOT_CONFIGURED",
                message="当前账号缺少 Casdoor 组织或应用信息。",
                status_code=503,
            )
        remote = await self._casdoor.find_user(
            email=destination if kind == "email" else None,
            phone=destination if kind == "phone" else None,
        )
        if remote is not None and str(remote.get("id") or "") not in {
            "",
            user.casdoor_user_id,
        }:
            raise ApplicationError(
                code="IDENTITY_LINK_CONFLICT",
                message="该联系方式已经被其他 Casdoor 用户使用。",
                status_code=409,
            )
        previous_value = user.email if kind == "email" else user.phone
        previous_verified = user.email_verified if kind == "email" else user.phone_verified
        challenge = ContactVerificationChallenge(
            id=uuid4(),
            user_id=user_id,
            kind=kind,
            destination=destination,
            country_code=country_code,
            previous_value=previous_value,
            previous_verified=previous_verified,
            status="pending",
            attempts=0,
            expected_revision=expected_revision,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        self._session.add(challenge)
        # Casdoor verify-code 按目标联系方式查找用户，因此先写入未验证联系方式；
        # 发送失败时立即回滚到旧值。
        try:
            await self._casdoor.update_profile(
                user.casdoor_user_id,
                email=destination if kind == "email" else None,
                phone=destination if kind == "phone" else None,
                email_verified=False if kind == "email" else None,
                phone_verified=False if kind == "phone" else None,
                access_token=access_token,
            )
            await self._casdoor.send_contact_code(
                kind=kind,
                destination=destination,
                country_code=country_code,
                # Casdoor 的 applicationId 是应用实体 ID（owner/name），
                # 不是用户所属组织/应用名；例如 admin/MomentOne。
                application_id=self._casdoor.application_id,
            )
        except ApplicationError:
            await self._restore_contact(user, challenge, access_token=access_token)
            raise
        await self._session.flush()
        await self._audit(
            user_id,
            event_type="account.contact.verification_started",
            resource_type="contact_challenge",
            resource_id=challenge.id,
            metadata={"kind": kind},
        )
        return {
            "challengeId": str(challenge.id),
            "kind": kind,
            "maskedDestination": self._mask(destination, kind),
            "expiresAt": challenge.expires_at.isoformat(),
        }

    async def confirm_contact_challenge(
        self,
        user_id: UUID,
        *,
        challenge_id: UUID,
        code: str,
        access_token: str | None = None,
    ) -> dict[str, object]:
        challenge = await self._session.scalar(
            select(ContactVerificationChallenge)
            .where(ContactVerificationChallenge.id == challenge_id)
            .with_for_update()
        )
        if challenge is None or challenge.user_id != user_id:
            raise ApplicationError(
                code="VERIFICATION_NOT_FOUND", message="验证会话不存在。", status_code=404
            )
        user = await self._user(user_id, lock=True)
        if user.revision != challenge.expected_revision:
            raise ApplicationError(
                code="REVISION_CONFLICT",
                message="账号资料已经变化，请重新发起验证。",
                status_code=409,
                details={"actualRevision": user.revision},
            )
        if challenge.status != "pending" or challenge.expires_at <= datetime.now(UTC):
            challenge.status = "expired"
            await self._restore_contact(user, challenge, access_token=access_token)
            raise ApplicationError(
                code="VERIFICATION_EXPIRED", message="验证码已失效，请重新发送。", status_code=409
            )
        challenge.attempts += 1
        remote_user = await self._casdoor.get_user(user.casdoor_user_id, access_token=access_token)
        organization = str(remote_user.get("owner") or self._settings.casdoor_organization or "")
        if not organization:
            raise ApplicationError(
                code="IDENTITY_VERIFICATION_NOT_CONFIGURED",
                message="当前账号缺少 Casdoor 组织信息。",
                status_code=503,
            )
        try:
            await self._casdoor.verify_contact_code(
                kind=challenge.kind,
                destination=challenge.destination,
                country_code=challenge.country_code,
                code=code,
                organization=organization,
            )
        except ApplicationError:
            if challenge.attempts >= 5:
                challenge.status = "failed"
                await self._restore_contact(user, challenge, access_token=access_token)
            raise ApplicationError(
                code="VERIFICATION_CODE_INVALID",
                message="验证码错误或已失效。",
                status_code=400,
                details={"remainingAttempts": max(0, 5 - challenge.attempts)},
            ) from None
        await self._casdoor.update_profile(
            user.casdoor_user_id,
            email=challenge.destination if challenge.kind == "email" else None,
            phone=challenge.destination if challenge.kind == "phone" else None,
            email_verified=True if challenge.kind == "email" else None,
            phone_verified=True if challenge.kind == "phone" else None,
            access_token=access_token,
        )
        if challenge.kind == "email":
            user.email = challenge.destination
            user.email_verified = True
        else:
            user.phone = challenge.destination
            user.phone_verified = True
        user.profile_sync_status = "synced"
        user.profile_synced_at = datetime.now(UTC)
        user.profile_sync_error = None
        user.revision += 1
        await self._identities.unlink_other_contacts(
            user_id=user_id,
            kind=challenge.kind,
            keep_subject=challenge.destination,
        )
        identity = await self._identities.upsert_contact(
            user_id=user_id,
            kind=challenge.kind,
            destination=challenge.destination,
        )
        if identity.user_id != user_id:
            raise ApplicationError(
                code="IDENTITY_LINK_CONFLICT",
                message="该联系方式已属于另一个账号。",
                status_code=409,
            )
        challenge.status = "verified"
        challenge.verified_at = datetime.now(UTC)
        await self._audit(
            user_id,
            event_type="account.contact.verified",
            resource_type="user_identity",
            resource_id=identity.id,
            metadata={"kind": challenge.kind},
        )
        account = await AccountRepository(self._session).account(user_id)
        assert account is not None
        return account

    async def cancel_contact_challenge(
        self, user_id: UUID, challenge_id: UUID, *, access_token: str | None = None
    ) -> None:
        challenge = await self._session.scalar(
            select(ContactVerificationChallenge)
            .where(ContactVerificationChallenge.id == challenge_id)
            .with_for_update()
        )
        if challenge is None or challenge.user_id != user_id:
            raise ApplicationError(
                code="VERIFICATION_NOT_FOUND", message="验证会话不存在。", status_code=404
            )
        if challenge.status == "pending":
            await self._restore_contact(
                await self._user(user_id), challenge, access_token=access_token
            )
            challenge.status = "canceled"

    async def start_link_session(
        self,
        user_id: UUID,
        *,
        provider: str | None,
        return_uri: str | None,
    ) -> dict[str, object]:
        await self._user(user_id)
        normalized_provider = normalize_login_provider(provider)
        if provider and normalized_provider is None:
            raise ApplicationError(
                code="IDENTITY_LINK_PROVIDER_UNSUPPORTED",
                message="暂不支持该登录方式。",
                status_code=400,
            )
        safe_return = self._safe_return_uri(return_uri)
        state = __import__("secrets").token_urlsafe(32)
        verifier = generate_pkce_verifier()
        session = AccountLinkSession(
            id=uuid4(),
            user_id=user_id,
            state=state,
            code_verifier=verifier,
            provider=normalized_provider,
            return_uri=safe_return,
            status="pending",
            metadata_={},
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        self._session.add(session)
        await self._session.flush()
        authorize_url = (
            await self._casdoor.provider_link_url(
                provider=normalized_provider,
                return_uri=self._link_return_uri(state),
            )
            if normalized_provider
            else self._casdoor.authorize_url(
                state=state,
                code_verifier=verifier,
                redirect_uri=self._link_callback_uri(),
            )
        )
        return {
            "linkSessionId": str(session.id),
            "authorizeUrl": authorize_url,
            "expiresAt": session.expires_at.isoformat(),
        }

    async def has_link_state(self, state: str) -> bool:
        return bool(
            await self._session.scalar(
                select(AccountLinkSession.id).where(
                    AccountLinkSession.state == state,
                    AccountLinkSession.status == "pending",
                )
            )
        )

    async def complete_link_session(
        self, *, state: str, code: str | None, error: str | None
    ) -> str:
        link = await self._session.scalar(
            select(AccountLinkSession).where(AccountLinkSession.state == state).with_for_update()
        )
        if link is None or link.status != "pending" or link.expires_at <= datetime.now(UTC):
            raise ApplicationError(
                code="IDENTITY_LINK_SESSION_EXPIRED",
                message="登录方式绑定会话已失效。",
                status_code=400,
            )
        if error or not code:
            link.status = "failed"
            link.error_code = error or "authorization_failed"
            return self._redirect(link.return_uri, link="failed", error=link.error_code)
        tokens = await self._casdoor.exchange_code(
            code=code,
            code_verifier=link.code_verifier,
            redirect_uri=self._link_callback_uri(),
        )
        access_token = str(tokens["access_token"])
        principal = self._verifier.verify(access_token)
        userinfo = await self._verifier.fetch_userinfo(access_token)
        principal = principal.merge_userinfo(userinfo)
        issuer = principal.issuer.rstrip("/")
        existing = await self._identities.get_by_external(
            issuer, principal.subject, active_only=False
        )
        now = datetime.now(UTC)
        if existing is not None and existing.user_id != link.user_id:
            link.status = "conflict"
            link.conflict_user_id = existing.user_id
            link.completed_at = now
            return self._redirect(
                link.return_uri,
                link="merge_required",
                session=str(link.id),
            )
        if link.provider:
            remote_user_id = str(userinfo.get("id") or "")
            remote_user = await self._casdoor.get_user(remote_user_id) if remote_user_id else {}
            if not has_casdoor_provider_link(remote_user, link.provider):
                link.status = "failed"
                link.error_code = "provider_not_linked"
                link.completed_at = now
                return self._redirect(
                    link.return_uri,
                    link="failed",
                    error=link.error_code,
                )
        identity = await self._identities.ensure_oidc(
            user_id=link.user_id,
            issuer=issuer,
            subject=principal.subject,
            identifier=principal.email or principal.phone or principal.username,
            display_name=principal.display_name,
            provider=link.provider or "casdoor",
            metadata={
                "owner": principal.owner,
                "username": principal.username,
                "email": principal.email,
                "phone": principal.phone,
                "casdoorUserId": userinfo.get("id"),
            },
        )
        link.linked_identity_id = identity.id
        link.status = "already_linked" if existing is not None else "linked"
        link.completed_at = now
        await self._audit(
            link.user_id,
            event_type="account.identity.linked",
            resource_type="user_identity",
            resource_id=identity.id,
            metadata={"provider": identity.provider},
        )
        return self._redirect(link.return_uri, link=link.status)

    async def complete_provider_link_session(self, *, state: str | None) -> str:
        link = await self._session.scalar(
            select(AccountLinkSession).where(AccountLinkSession.state == state).with_for_update()
        )
        if link is None or link.status != "pending" or link.expires_at <= datetime.now(UTC):
            raise ApplicationError(
                code="IDENTITY_LINK_SESSION_EXPIRED",
                message="登录方式绑定会话已失效。",
                status_code=400,
            )
        now = datetime.now(UTC)
        if not link.provider:
            link.status = "failed"
            link.error_code = "provider_not_linked"
            link.completed_at = now
            return self._redirect(link.return_uri, link="failed", error=link.error_code)

        user = await self._user(link.user_id)
        remote_user = await self._casdoor.get_user(user.casdoor_user_id)
        if not has_casdoor_provider_link(remote_user, link.provider):
            link.status = "failed"
            link.error_code = "provider_not_linked"
            link.completed_at = now
            return self._redirect(link.return_uri, link="failed", error=link.error_code)

        issuer = str(self._settings.casdoor_issuer or "casdoor").rstrip("/")
        existing = await self._identities.get_by_external(
            issuer, user.casdoor_sub, active_only=False
        )
        identity = await self._identities.ensure_oidc(
            user_id=link.user_id,
            issuer=issuer,
            subject=user.casdoor_sub,
            identifier=user.email or user.phone or user.display_name,
            display_name=user.display_name,
            provider=link.provider,
            metadata={"casdoorUserId": user.casdoor_user_id},
        )
        link.linked_identity_id = identity.id
        link.status = "already_linked" if existing is not None else "linked"
        link.completed_at = now
        await self._audit(
            link.user_id,
            event_type="account.identity.linked",
            resource_type="user_identity",
            resource_id=identity.id,
            metadata={"provider": link.provider},
        )
        return self._redirect(link.return_uri, link=link.status)

    async def unlink_preview(self, user_id: UUID, identity_id: UUID) -> dict[str, object]:
        identity = await self._identities.get(identity_id)
        if identity is None or identity.user_id != user_id or identity.status != "active":
            raise ApplicationError(
                code="IDENTITY_NOT_FOUND", message="登录方式不存在。", status_code=404
            )
        active_oidc = [
            item
            for item in await self._identities.list_for_user(user_id)
            if item.identity_type == "oidc"
        ]
        if identity.identity_type == "oidc" and len(active_oidc) <= 1:
            raise ApplicationError(
                code="LAST_IDENTITY_CANNOT_UNLINK",
                message="不能解除最后一个可用登录方式。",
                status_code=409,
            )
        confirmation = await self._confirmations.create(
            user_id=user_id,
            target_type="user_identity",
            target_id=identity.id,
            action="unlink_identity",
            expected_revision=identity.revision,
            preview={
                "identity": self._identity_dict(identity),
                "confirmationPhrase": UNLINK_PHRASE,
            },
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        return {
            "confirmationId": str(confirmation.id),
            "expiresAt": confirmation.expires_at.isoformat(),
            "confirmationPhrase": UNLINK_PHRASE,
            "identity": self._identity_dict(identity),
            "warning": "解除后将不能再使用该登录方式进入 Moment One。",
        }

    async def unlink_confirm(
        self,
        user_id: UUID,
        *,
        confirmation_id: UUID,
        phrase: str,
        issued_at: int | None,
    ) -> dict[str, object]:
        self._require_recent_auth(issued_at)
        if phrase.strip() != UNLINK_PHRASE:
            raise ApplicationError(
                code="CONFIRMATION_PHRASE_INVALID",
                message=f"请输入“{UNLINK_PHRASE}”确认解除。",
                status_code=400,
            )
        confirmation = await self._confirmations.get(confirmation_id)
        if (
            confirmation is None
            or confirmation.user_id != user_id
            or confirmation.action != "unlink_identity"
            or confirmation.status != "pending"
            or confirmation.expires_at <= datetime.now(UTC)
        ):
            raise ApplicationError(
                code="CONFIRMATION_EXPIRED", message="解除确认已失效。", status_code=409
            )
        identity = await self._identities.get(confirmation.target_id)
        if identity is None or identity.user_id != user_id:
            raise ApplicationError(
                code="IDENTITY_NOT_FOUND", message="登录方式不存在。", status_code=404
            )
        if identity.revision != confirmation.expected_revision:
            raise ApplicationError(
                code="REVISION_CONFLICT", message="登录方式已变化，请重新确认。", status_code=409
            )
        active_oidc = [
            item
            for item in await self._identities.list_for_user(user_id)
            if item.identity_type == "oidc"
        ]
        if identity.identity_type == "oidc" and len(active_oidc) <= 1:
            raise ApplicationError(
                code="LAST_IDENTITY_CANNOT_UNLINK",
                message="不能解除最后一个可用登录方式。",
                status_code=409,
            )
        await self._identities.unlink(identity)
        await self._confirmations.mark_used(
            confirmation_id=confirmation.id, used_at=datetime.now(UTC)
        )
        await self._audit(
            user_id,
            event_type="account.identity.unlinked",
            resource_type="user_identity",
            resource_id=identity.id,
        )
        return {"unlinked": True, "identityId": str(identity.id)}

    async def merge_preview(self, user_id: UUID, link_session_id: UUID) -> dict[str, object]:
        link = await self._session.scalar(
            select(AccountLinkSession).where(AccountLinkSession.id == link_session_id)
        )
        if (
            link is None
            or link.user_id != user_id
            or link.status != "conflict"
            or link.conflict_user_id is None
        ):
            raise ApplicationError(
                code="ACCOUNT_MERGE_REQUIRED",
                message="没有需要处理的账号合并冲突。",
                status_code=409,
            )
        current = await AccountRepository(self._session).admin_user_detail(user_id)
        conflict = await AccountRepository(self._session).admin_user_detail(link.conflict_user_id)
        if current is None or conflict is None:
            raise ApplicationError(
                code="USER_NOT_FOUND", message="冲突账号不存在。", status_code=404
            )
        return {
            "linkSessionId": str(link.id),
            "status": "merge_required",
            "survivingUser": self._merge_side(current),
            "sourceUser": self._merge_side(conflict),
            "conflicts": [
                "订阅、余额和管理员 Grant 需要人工确认后才能合并。",
                "设备与 MCP 授权不会在预览阶段迁移。",
            ],
            "canAutoMerge": False,
        }

    async def _restore_contact(
        self,
        user: User,
        challenge: ContactVerificationChallenge,
        *,
        access_token: str | None = None,
    ) -> None:
        try:
            remote = await self._casdoor.get_user(user.casdoor_user_id, access_token=access_token)
            if challenge.kind == "email":
                remote["email"] = challenge.previous_value or ""
                remote["emailVerified"] = challenge.previous_verified
            else:
                remote["phone"] = challenge.previous_value or ""
                remote["phoneVerified"] = challenge.previous_verified
            await self._casdoor.update_user(remote, access_token=access_token)
        except ApplicationError:
            user.profile_sync_status = "failed"
            user.profile_sync_error = "联系方式回滚失败，需要管理员处理。"

    def _link_callback_uri(self) -> str:
        if self._settings.account_link_redirect_uri:
            return self._settings.account_link_redirect_uri
        if self._settings.casdoor_mcp_redirect_uri:
            return self._settings.casdoor_mcp_redirect_uri
        base = self._settings.mcp_base_url or "http://127.0.0.1:8000"
        return f"{base.rstrip('/')}/oauth/callback"

    def _link_return_uri(self, state: str) -> str:
        if self._settings.account_link_redirect_uri:
            parsed = urlparse(self._settings.account_link_redirect_uri)
            base = f"{parsed.scheme}://{parsed.netloc}"
        elif self._settings.casdoor_mcp_redirect_uri:
            parsed = urlparse(self._settings.casdoor_mcp_redirect_uri)
            base = f"{parsed.scheme}://{parsed.netloc}"
        else:
            base = self._settings.mcp_base_url or "http://127.0.0.1:8000"
        return f"{base.rstrip()}/oauth/link-return?{urlencode({'state': state})}"

    def _safe_return_uri(self, value: str | None) -> str:
        base = self._settings.web_base_url.rstrip("/")
        allowed_origins = {base}
        allowed_origins.update(origin.rstrip("/") for origin in self._settings.allowed_origins)
        default_origin = next(
            (origin for origin in allowed_origins if origin.startswith("https://")),
            base,
        )
        default = f"{default_origin}/space/settings/?section=security"
        if not value:
            return default
        parsed = urlparse(value)
        requested_origin = f"{parsed.scheme}://{parsed.netloc}"
        if requested_origin in allowed_origins:
            return value
        return default

    @staticmethod
    def _redirect(base: str, **params: object) -> str:
        delimiter = "&" if "?" in base else "?"
        return f"{base}{delimiter}{urlencode({key: str(value) for key, value in params.items()})}"

    @staticmethod
    def _require_recent_auth(issued_at: int | None) -> None:
        now = datetime.now(UTC).timestamp()
        if issued_at is None or now - issued_at > 300 or issued_at > now + 30:
            raise ApplicationError(
                code="REAUTHENTICATION_REQUIRED",
                message="该操作需要重新验证登录密码。",
                status_code=401,
                details={"maxAgeSeconds": 300},
            )

    @staticmethod
    def _normalize_contact(kind: str, value: str) -> str:
        normalized = value.strip()
        if kind == "email":
            normalized = normalized.lower()
            if "@" not in normalized or len(normalized) > 255:
                raise ApplicationError(
                    code="INVALID_EMAIL", message="邮箱格式不正确。", status_code=400
                )
        elif kind == "phone":
            normalized = "".join(ch for ch in normalized if ch.isdigit() or ch == "+")
            if len(normalized) < 6 or len(normalized) > 24:
                raise ApplicationError(
                    code="INVALID_PHONE", message="手机号格式不正确。", status_code=400
                )
        else:
            raise ApplicationError(
                code="INVALID_REQUEST", message="联系方式类型不支持。", status_code=400
            )
        return normalized

    @staticmethod
    def _mask(value: str, kind: str) -> str:
        if kind == "email" and "@" in value:
            name, domain = value.split("@", 1)
            return f"{name[:2]}***@{domain}"
        if len(value) <= 4:
            return "****"
        return f"{value[:3]}****{value[-3:]}"

    @staticmethod
    def _identity_dict(item: UserIdentity) -> dict[str, object]:
        return {
            "id": str(item.id),
            "type": item.identity_type,
            "provider": item.provider,
            "identifier": item.identifier,
            "displayName": item.display_name,
            "status": item.status,
            "isPrimary": item.is_primary,
            "verifiedAt": item.verified_at.isoformat() if item.verified_at else None,
            "lastSeenAt": item.last_seen_at.isoformat(),
            "revision": item.revision,
        }

    @staticmethod
    def _merge_side(detail: dict[str, object]) -> dict[str, object]:
        user = detail["user"]
        plan = detail["plan"]
        storage = detail["storage"]
        access = detail["access"]
        usage = detail["quotaUsage30d"]
        return {
            "user": user,
            "plan": plan,
            "storage": storage,
            "access": access,
            "usage30d": usage,
        }


__all__ = ["AccountCenterService", "UNLINK_PHRASE"]
