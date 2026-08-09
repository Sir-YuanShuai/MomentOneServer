from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.infrastructure.database.models import PlanDefinition, User
from app.infrastructure.database.session import Database
from app.modules.account_deletion.service import AccountDeletionService
from app.modules.admin.plans import AdminPlanRepository
from app.modules.entitlements.repository import EntitlementRepository
from app.modules.quotas.repository import QuotaRepository
from sqlalchemy import select


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quota_idempotency_and_account_deletion() -> None:
    settings = Settings()
    if not settings.database_url:
        pytest.skip("MOMENT_ONE_DATABASE_URL is not configured")

    database = Database(settings)
    try:
        async with database.session_factory() as session:
            user_id = uuid4()
            user = User(
                id=user_id,
                casdoor_sub=f"integration-{user_id}",
                casdoor_user_id=str(user_id),
                display_name="Quota Test",
                status="active",
                revision=1,
            )
            session.add(user)
            await session.flush()
            await EntitlementRepository(session).ensure_user_defaults(user_id)

            free = await session.scalar(
                select(PlanDefinition).where(PlanDefinition.key == "free").with_for_update()
            )
            assert free is not None
            free.quotas = {**free.quotas, "mcp.tool_calls.month": 1}
            await session.flush()

            quotas = QuotaRepository(session)
            first = await quotas.consume(
                user_id,
                "mcp.tool_calls.month",
                amount=1,
                operation_key="integration:one",
                actor_type="mcp",
                tool_name="moments_list",
            )
            replay = await quotas.consume(
                user_id,
                "mcp.tool_calls.month",
                amount=1,
                operation_key="integration:one",
                actor_type="mcp",
                tool_name="moments_list",
            )
            assert first.used_value == replay.used_value == 1
            with pytest.raises(ApplicationError) as exc_info:
                await quotas.consume(
                    user_id,
                    "mcp.tool_calls.month",
                    amount=1,
                    operation_key="integration:two",
                    actor_type="mcp",
                    tool_name="moments_list",
                )
            assert exc_info.value.code == "QUOTA_EXCEEDED"

            plan_entitlements = {
                key: value for key, value in free.entitlements.items() if isinstance(value, bool)
            }
            plan_entitlements["history.extended"] = True
            plan_quotas = {
                key: value
                for key, value in free.quotas.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
            plan_quotas["storage_bytes"] = 2 * 1024**3
            updated_plan = await AdminPlanRepository(session).update(
                key="free",
                expected_version=free.version,
                name=None,
                status=None,
                entitlements=plan_entitlements,
                quotas=plan_quotas,
            )
            assert updated_plan is not None
            storage_account = await EntitlementRepository(session).locked_account(user_id)
            active_entitlements = await quotas.active_entitlements(user_id)
            assert storage_account.effective_quota_bytes == 2 * 1024**3
            assert "history.extended" in active_entitlements

            deletion = AccountDeletionService(session, storage=None)
            preview = await deletion.preview(user_id)
            result = await deletion.confirm(
                user_id,
                confirmation_id=UUID(str(preview["confirmationId"])),
                confirmation_phrase="永久注销",
                issued_at=int(datetime.now(UTC).timestamp()),
            )
            assert result["deleted"] is True
            assert await session.scalar(select(User).where(User.id == user_id)) is None
            await session.rollback()
    finally:
        await database.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plan_change_is_immediately_visible_in_account_snapshot() -> None:
    """Changing Free -> Pro must refresh storage and periodic quota snapshots."""
    settings = Settings()
    if not settings.database_url:
        pytest.skip("MOMENT_ONE_DATABASE_URL is not configured")

    database = Database(settings)
    try:
        user_id = uuid4()
        async with database.session_factory() as session:
            session.add(
                User(
                    id=user_id,
                    casdoor_sub=f"plan-refresh-{user_id}",
                    casdoor_user_id=str(user_id),
                    display_name="Plan Refresh Test",
                    status="active",
                    revision=1,
                )
            )
            await session.flush()
            entitlements = EntitlementRepository(session)
            account = await entitlements.ensure_user_defaults(user_id)
            # Materialize Free quota rows so the Pro assignment exercises the
            # existing-row update path used by production accounts.
            await QuotaRepository(session).ensure_current_accounts(user_id)
            changed = await entitlements.set_plan(
                user_id,
                plan_key="pro",
                expected_revision=account.revision,
            )
            assert changed is not None
            await session.commit()

        async with database.session_factory() as session:
            from app.modules.admin.account import AccountRepository

            snapshot = await AccountRepository(session).account(user_id)
            assert snapshot is not None
            assert snapshot["plan"]["key"] == "pro"  # type: ignore[index]
            assert snapshot["plan"]["name"] == "Pro"  # type: ignore[index]
            assert snapshot["storage"]["effectiveQuotaBytes"] == 50 * 1024**3  # type: ignore[index]
            quotas = {
                item["quotaKey"]: item["limit"]  # type: ignore[index]
                for item in snapshot["quotaAccounts"]  # type: ignore[union-attr]
            }
            assert quotas["api.requests.month"] == 500_000
            assert quotas["mcp.tool_calls.month"] == 100_000
            await session.rollback()
    finally:
        await database.dispose()
