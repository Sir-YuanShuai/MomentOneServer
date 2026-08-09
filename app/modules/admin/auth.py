from dataclasses import dataclass
from uuid import UUID

import httpx
from fastapi import Depends, Header

from app.api.deps import AuthContext, get_auth_context
from app.core.config import Settings, get_settings
from app.core.errors import ApplicationError
from app.infrastructure.identity.casdoor import AuthenticatedPrincipal, CasdoorTokenVerifier

ALL_ADMIN_PERMISSIONS = frozenset(
    {"admin.read", "admin.users", "admin.security", "admin.operations"}
)
OPERATOR_PERMISSIONS = frozenset({"admin.read", "admin.security", "admin.operations"})


@dataclass(frozen=True, slots=True)
class AdminContext:
    user_id: UUID
    subject: str
    display_name: str | None
    email: str | None
    permissions: frozenset[str]
    is_super_admin: bool
    source: str

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise ApplicationError(
                code="ADMIN_PERMISSION_DENIED",
                message="当前管理员没有执行此操作的权限。",
                status_code=403,
                details={"requiredPermission": permission},
            )


def permissions_for_principal(
    principal: AuthenticatedPrincipal,
    settings: Settings,
) -> tuple[frozenset[str], bool, str | None]:
    roles = set(principal.roles)
    if principal.is_admin:
        return ALL_ADMIN_PERMISSIONS, True, "casdoor.isAdmin"
    if roles.intersection(settings.casdoor_admin_roles):
        return ALL_ADMIN_PERMISSIONS, True, "casdoor.role"
    direct = ALL_ADMIN_PERMISSIONS.intersection(principal.permissions)
    if roles.intersection(settings.casdoor_operator_roles):
        direct = direct.union(OPERATOR_PERMISSIONS)
    if direct:
        return frozenset(direct), False, "casdoor.permission"
    return frozenset(), False, None


async def get_admin_context(
    auth: AuthContext = Depends(get_auth_context),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> AdminContext:
    if auth.method != "casdoor" or not authorization:
        raise ApplicationError(
            code="ADMIN_REQUIRED",
            message="管理后台仅允许 Casdoor 管理员访问。",
            status_code=403,
        )
    token = authorization.removeprefix("Bearer ").strip()
    verifier = CasdoorTokenVerifier(settings)
    principal = verifier.verify(token)
    permissions, is_super, source = permissions_for_principal(principal, settings)
    if not permissions:
        try:
            principal = principal.merge_userinfo(await verifier.fetch_userinfo(token))
        except httpx.HTTPError:
            raise ApplicationError(
                code="ADMIN_IDENTITY_UNAVAILABLE",
                message="暂时无法从 Casdoor 确认管理员身份。",
                status_code=503,
            ) from None
        permissions, is_super, source = permissions_for_principal(principal, settings)
    if not permissions or source is None:
        raise ApplicationError(
            code="ADMIN_REQUIRED",
            message="当前账号不是 Moment One 管理员。",
            status_code=403,
        )
    return AdminContext(
        user_id=auth.user_id,
        subject=principal.subject,
        display_name=principal.display_name,
        email=principal.email,
        permissions=permissions,
        is_super_admin=is_super,
        source=source,
    )


__all__ = ["AdminContext", "get_admin_context"]
