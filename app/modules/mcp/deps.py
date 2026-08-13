"""MCP 工具运行环境：身份、Scope、Entitlement、Quota、事务与错误映射。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApplicationError
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.storage.object_storage import ObjectStorage
from app.modules.mcp.a2ui import A2UI_DISABLED, A2UISupport
from app.modules.mcp.scope import has_scope
from app.modules.mcp.tools import McpCallContext, err_result
from app.modules.quotas.repository import QuotaRepository
from app.modules.quotas.tool_policy import TOOL_POLICIES

logger = structlog.get_logger()

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class McpToolEnv:
    """一次 MCPServer 实例的工具执行环境。"""

    def __init__(
        self,
        *,
        session_factory: SessionFactory | None = None,
        enforce_quotas: bool = True,
        object_storage: ObjectStorage | None = None,
        max_upload_bytes: int = 20 * 1024 * 1024,
        upload_url_ttl_seconds: int = 600,
    ) -> None:
        self._session_factory: SessionFactory = session_factory or _default_session_factory
        self._enforce_quotas = enforce_quotas
        self._object_storage = object_storage
        self._max_upload_bytes = max_upload_bytes
        self._upload_url_ttl_seconds = upload_url_ttl_seconds

    def session(self) -> AbstractAsyncContextManager[AsyncSession]:
        return self._session_factory()

    async def visible_tool_names(self, user_id: UUID, scopes: tuple[str, ...]) -> frozenset[str]:
        if not self._enforce_quotas:
            return frozenset(
                name for name, policy in TOOL_POLICIES.items() if has_scope(scopes, policy.scope)
            )
        async with self._session_factory() as session:
            quota_repo = QuotaRepository(session)
            entitlements = await quota_repo.active_entitlements(user_id)
            visible: set[str] = set()
            base_available = await quota_repo.check_available(user_id, "mcp.tool_calls.month")
            for tool_name, policy in TOOL_POLICIES.items():
                if not has_scope(scopes, policy.scope):
                    continue
                if policy.entitlement and policy.entitlement not in entitlements:
                    continue
                if policy.metered and not base_available:
                    continue
                if policy.write and not await quota_repo.check_available(
                    user_id, "mcp.write_calls.month"
                ):
                    continue
                if policy.planner and not await quota_repo.check_available(
                    user_id, "mcp.agent_plan.day"
                ):
                    continue
                visible.add(tool_name)
            await session.rollback()
            return frozenset(visible)

    async def call(
        self,
        fn: Callable[[McpCallContext], Awaitable[object]],
        *,
        tool_name: str,
        idempotency_key: str | None = None,
        a2ui_support: A2UISupport = A2UI_DISABLED,
    ) -> object:
        """执行 Tool：先做 Scope/Entitlement/Quota 判断，再执行业务并统一提交。"""
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        if token is None:
            return err_result("AUTH_REQUIRED", "缺少认证上下文，请携带 Bearer Token 重试。")

        subject = token.subject
        try:
            user_id = UUID(subject) if subject else None
        except (ValueError, TypeError):
            user_id = None
        if user_id is None:
            return err_result("TOKEN_INVALID", "Token 未携带有效用户身份。")

        scopes = tuple(token.scopes or [])
        claims = token.claims or {}
        method = str(claims.get("method", "mcp"))
        client_id = claims.get("client_id") or token.client_id
        device_id = claims.get("device_id")
        actor_id = client_id or device_id
        request_id = str(uuid4())
        policy = TOOL_POLICIES.get(tool_name)

        async with self._session_factory() as session:
            quota_repo = QuotaRepository(session)
            account = await UserRepository(session).get(user_id) if self._enforce_quotas else None
            ctx = McpCallContext(
                user_id=user_id,
                scopes=scopes,
                method=method,
                actor_id=str(actor_id) if actor_id else None,
                request_id=request_id,
                session=session,
                account_timezone=account.timezone if account else None,
                # A2UI 是眼镜渲染通道；普通 MCP / MCP Apps 始终使用 HTML App。
                a2ui=a2ui_support if method == "glasses" else A2UI_DISABLED,
                object_storage=self._object_storage,
                max_upload_bytes=self._max_upload_bytes,
                upload_url_ttl_seconds=self._upload_url_ttl_seconds,
            )
            try:
                if policy is not None:
                    if not has_scope(scopes, policy.scope):
                        raise ApplicationError(
                            code="SCOPE_DENIED",
                            message=f"Token 缺少 {policy.scope} 权限，无法执行该工具。",
                            status_code=403,
                            details={"requiredScope": policy.scope, "scopes": list(scopes)},
                        )
                    entitlements = (
                        await quota_repo.active_entitlements(user_id)
                        if self._enforce_quotas
                        else frozenset({policy.entitlement} if policy.entitlement else set())
                    )
                    if policy.entitlement and policy.entitlement not in entitlements:
                        raise ApplicationError(
                            code="ENTITLEMENT_REQUIRED",
                            message="当前订阅不包含该工具能力。",
                            status_code=403,
                            details={
                                "toolName": tool_name,
                                "requiredEntitlement": policy.entitlement,
                            },
                        )
                    if policy.metered and self._enforce_quotas:
                        operation = f"mcp:{tool_name}:{idempotency_key or request_id}"

                        async def consume(quota_key: str) -> None:
                            await quota_repo.consume(
                                user_id,
                                quota_key,
                                amount=1,
                                operation_key=operation,
                                actor_type=method,
                                tool_name=tool_name,
                                client_id=str(client_id) if client_id else None,
                                device_id=str(device_id) if device_id else None,
                                idempotency_key=idempotency_key,
                            )

                        await consume("mcp.tool_calls.month")
                        if policy.write:
                            await consume("mcp.write_calls.month")
                        if policy.planner:
                            await consume("mcp.agent_plan.day")
                ctx.available_tools = (
                    await self._visible_tool_names_in_session(quota_repo, user_id, scopes)
                    if self._enforce_quotas
                    else frozenset(
                        name
                        for name, item in TOOL_POLICIES.items()
                        if has_scope(scopes, item.scope)
                    )
                )
                result = await fn(ctx)
                await session.commit()
                return result
            except ApplicationError as exc:
                await session.rollback()
                await logger.ainfo(
                    "mcp_tool_rejected",
                    code=exc.code,
                    tool_name=tool_name,
                    user_id=str(user_id),
                    request_id=ctx.request_id,
                )
                return err_result(exc.code, exc.message, exc.details)
            except Exception:
                await session.rollback()
                await logger.aexception(
                    "mcp_tool_failed",
                    tool_name=tool_name,
                    user_id=str(user_id),
                    request_id=ctx.request_id,
                )
                return err_result("INTERNAL_ERROR", "服务器内部错误，请稍后重试。")

    async def _visible_tool_names_in_session(
        self,
        quota_repo: QuotaRepository,
        user_id: UUID,
        scopes: tuple[str, ...],
    ) -> frozenset[str]:
        entitlements = await quota_repo.active_entitlements(user_id)
        base_available = await quota_repo.check_available(user_id, "mcp.tool_calls.month")
        visible: set[str] = set()
        for name, policy in TOOL_POLICIES.items():
            if not has_scope(scopes, policy.scope):
                continue
            if policy.entitlement and policy.entitlement not in entitlements:
                continue
            if policy.metered and not base_available:
                continue
            if policy.write and not await quota_repo.check_available(
                user_id, "mcp.write_calls.month"
            ):
                continue
            if policy.planner and not await quota_repo.check_available(
                user_id, "mcp.agent_plan.day"
            ):
                continue
            visible.add(name)
        return frozenset(visible)


@asynccontextmanager
async def _default_session_factory() -> AsyncGenerator[AsyncSession]:
    from app.infrastructure.database.session import get_database

    async with get_database().session_factory() as session:
        yield session


__all__ = ["McpToolEnv"]
