from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_authenticated_user_id
from app.infrastructure.database.repositories.moment_repository import PostgresMomentRepository
from app.infrastructure.database.session import get_db_session

router = APIRouter(prefix="/v1/insights", tags=["insights"])


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _amount(payload: dict) -> float:
    value = payload.get("amount")
    return float(value) if isinstance(value, int | float) else 0.0


@router.get("/bookkeeping")
async def bookkeeping_insights(
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    ledger: str | None = Query(default=None, max_length=30),
) -> dict:
    start = _date(from_)
    end = _date(to)
    moments = await PostgresMomentRepository(session).list_by_type_and_time(
        user_id, "bookkeeping", occurred_from=start, occurred_to=end
    )
    ledgers: set[str] = set()
    expense = income = 0.0
    count = 0
    category: defaultdict[str, float] = defaultdict(float)
    ledger_share: defaultdict[str, float] = defaultdict(float)
    merchant: defaultdict[str, float] = defaultdict(float)
    account: defaultdict[str, float] = defaultdict(float)
    trend: defaultdict[str, dict[str, float]] = defaultdict(lambda: {"expense": 0, "income": 0})
    span_days = (end - start).days if start and end else 10_000
    by_day = span_days <= 45
    for moment in moments:
        payload = moment.payload
        source_ledger = str(payload.get("ledger") or "")
        if source_ledger:
            ledgers.add(source_ledger)
        if ledger and source_ledger != ledger:
            continue
        if payload.get("countInFlow") is False:
            continue
        amount = _amount(payload)
        flow = "income" if payload.get("flow") == "income" else "expense"
        count += 1
        if flow == "income":
            income += amount
        else:
            expense += amount
            category[str(payload.get("category") or "未分类")] += amount
            ledger_share[source_ledger or "未分类账本"] += amount
            source_merchant = str(payload.get("merchant") or "").strip()
            if source_merchant:
                merchant[source_merchant] += amount
        account[str(payload.get("account") or "未指定")] += amount
        key = (
            moment.occurred_at.strftime("%Y-%m-%d")
            if by_day
            else moment.occurred_at.strftime("%Y-%m")
        )
        trend[key][flow] += amount

    def shares(values: dict[str, float], limit: int | None = None) -> list[dict]:
        result = [
            {"name": name, "value": value}
            for name, value in sorted(values.items(), key=lambda item: item[1], reverse=True)
        ]
        return result[:limit] if limit else result

    return {
        "range": {"from": from_, "to": to},
        "summary": {
            "expense": expense,
            "income": income,
            "balance": income - expense,
            "count": count,
        },
        "ledgers": sorted(ledgers),
        "trend": [{"key": key, **values} for key, values in sorted(trend.items())],
        "categoryShare": shares(category),
        "ledgerShare": shares(ledger_share),
        "merchantTop": shares(merchant, 5),
        "accountShare": shares(account),
    }


@router.get("/overview")
async def overview_insights(
    user_id: UUID = Depends(get_authenticated_user_id),
    session: AsyncSession = Depends(get_db_session),
    from_: str = Query(alias="from"),
    to: str = Query(),
) -> dict:
    repo = PostgresMomentRepository(session)
    start, end = _date(from_), _date(to)
    bills = await repo.list_by_type_and_time(
        user_id, "bookkeeping", occurred_from=start, occurred_to=end
    )
    habits = await repo.list_by_type_and_time(
        user_id, "habit", occurred_from=start, occurred_to=end
    )
    expense = income = 0.0
    bill_count = 0
    for moment in bills:
        if moment.payload.get("countInFlow") is False:
            continue
        bill_count += 1
        if moment.payload.get("flow") == "income":
            income += _amount(moment.payload)
        else:
            expense += _amount(moment.payload)
    completed_habits = sum(1 for moment in habits if moment.payload.get("done") is True)
    return {
        "bookkeeping": {
            "expense": expense,
            "income": income,
            "balance": income - expense,
            "count": bill_count,
        },
        "habits": {"completedCount": completed_habits},
    }
