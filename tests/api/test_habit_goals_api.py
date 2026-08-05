"""习惯目标（habit-goals）路由 API 测试。

通过 dependency_overrides 注入 Fake AuthContext 和内存态 Repository，
不依赖数据库和 Casdoor。覆盖：
- POST /v1/habit-goals 创建
- GET /v1/habit-goals 列表
- GET /v1/habit-goals/{id} 详情
- PATCH /v1/habit-goals/{id} 更新（含 revision conflict）
- POST delete-preview + delete-confirm 两阶段删除
- 打卡记录 payload.goalId 关联校验（在 test_moments_api 中覆盖）
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.api import deps as deps_module
from app.api.routes import habit_goals as habit_goals_routes
from app.application import create_application
from app.core.config import Settings
from app.infrastructure.database.repositories.confirmation_repository import (
    PendingConfirmation,
)
from app.modules.habit_goals.domain import HabitGoal
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

USER_ID = UUID("11111111-1111-4111-8111-111111111111")


def _generate_rsa_keypair(tmp_path: Any) -> tuple[Any, Any]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path = tmp_path / "jwt_private.pem"
    pub_path = tmp_path / "jwt_public.pem"
    priv_path.write_bytes(priv_pem)
    pub_path.write_bytes(pub_pem)
    return priv_path, pub_path


def _make_settings(tmp_path: Any) -> Settings:
    priv_path, pub_path = _generate_rsa_keypair(tmp_path)
    return Settings(
        env="test",
        jwt_private_key_path=str(priv_path),
        jwt_public_key_path=str(pub_path),
        jwt_issuer="https://momentone.test",
        jwt_audience="momentone-glasses",
        access_token_ttl_seconds=3600,
        refresh_token_ttl_seconds=90 * 24 * 3600,
        binding_code_ttl_seconds=300,
        binding_code_length=24,
    )


class FakeHabitGoalRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, HabitGoal] = {}

    async def get_by_id(self, goal_id: UUID, user_id: UUID) -> HabitGoal | None:
        g = self._store.get(goal_id)
        if g is None or g.user_id != user_id or g.deleted_at is not None:
            return None
        return g

    async def list_by_user(self, user_id: UUID) -> list[HabitGoal]:
        return [g for g in self._store.values() if g.user_id == user_id and g.deleted_at is None]

    async def create(self, goal: HabitGoal) -> HabitGoal:
        self._store[goal.id] = goal
        return goal

    async def update(self, goal_id: UUID, user_id: UUID, **fields: Any) -> HabitGoal | None:
        g = self._store.get(goal_id)
        if g is None or g.user_id != user_id or g.deleted_at is not None:
            return None
        updated = HabitGoal(
            id=g.id,
            user_id=g.user_id,
            name=fields.get("name", g.name),
            unit=fields.get("unit", g.unit),
            frequency=fields.get("frequency", g.frequency),
            times_per_week=fields.get("times_per_week", g.times_per_week),
            color=fields.get("color", g.color),
            revision=g.revision + 1,
            created_at=g.created_at,
            updated_at=datetime.now(UTC),
            deleted_at=g.deleted_at,
        )
        self._store[goal_id] = updated
        return updated

    async def soft_delete(self, goal_id: UUID, user_id: UUID) -> HabitGoal | None:
        g = self._store.get(goal_id)
        if g is None or g.user_id != user_id or g.deleted_at is not None:
            return None
        deleted = HabitGoal(
            id=g.id,
            user_id=g.user_id,
            name=g.name,
            unit=g.unit,
            revision=g.revision + 1,
            created_at=g.created_at,
            updated_at=datetime.now(UTC),
            deleted_at=datetime.now(UTC),
        )
        self._store[goal_id] = deleted
        return deleted


class FakeConfirmationRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, PendingConfirmation] = {}

    async def create(
        self,
        *,
        user_id: UUID,
        target_type: str,
        target_id: UUID,
        action: str,
        expected_revision: int,
        preview: dict,
        expires_at: datetime,
    ) -> PendingConfirmation:
        cid = uuid4()
        c = PendingConfirmation(
            id=cid,
            user_id=user_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            expected_revision=expected_revision,
            status="pending",
            preview=preview,
            created_at=datetime.now(UTC),
            expires_at=expires_at,
            used_at=None,
        )
        self._store[cid] = c
        return c

    async def get(self, confirmation_id: UUID) -> PendingConfirmation | None:
        return self._store.get(confirmation_id)

    async def mark_used(self, *, confirmation_id: UUID, used_at: datetime) -> None:
        c = self._store.get(confirmation_id)
        if c is None:
            return
        self._store[confirmation_id] = PendingConfirmation(
            id=c.id,
            user_id=c.user_id,
            target_type=c.target_type,
            target_id=c.target_id,
            action=c.action,
            expected_revision=c.expected_revision,
            status="used",
            preview=c.preview,
            created_at=c.created_at,
            expires_at=c.expires_at,
            used_at=used_at,
        )


class FakeSession:
    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


@pytest.fixture
def fake_repos() -> dict[str, Any]:
    return {
        "goal": FakeHabitGoalRepository(),
        "confirmation": FakeConfirmationRepository(),
    }


@pytest.fixture
def app(tmp_path: Any, fake_repos: dict[str, Any]) -> Iterator[FastAPI]:
    settings = _make_settings(tmp_path)
    application = create_application(settings)

    async def _fake_user_id() -> UUID:
        return USER_ID

    async def _fake_session() -> FakeSession:
        return FakeSession()

    original_goal_repo = habit_goals_routes.SqlHabitGoalRepository
    original_confirmation_repo = habit_goals_routes.SqlConfirmationRepository

    habit_goals_routes.SqlHabitGoalRepository = lambda session: fake_repos["goal"]  # type: ignore[assignment]
    habit_goals_routes.SqlConfirmationRepository = lambda session: fake_repos["confirmation"]  # type: ignore[assignment]

    application.dependency_overrides[deps_module.get_authenticated_user_id] = _fake_user_id
    application.dependency_overrides[deps_module.get_db_session] = _fake_session

    yield application

    habit_goals_routes.SqlHabitGoalRepository = original_goal_repo  # type: ignore[assignment]
    habit_goals_routes.SqlConfirmationRepository = original_confirmation_repo  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_create_and_get_habit_goal(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/habit-goals",
            json={
                "name": "游泳",
                "unit": "次",
                "frequency": "weekly",
                "timesPerWeek": 3,
                "color": "#3b82f6",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "游泳"
        assert body["unit"] == "次"
        assert body["frequency"] == "weekly"
        assert body["timesPerWeek"] == 3
        assert body["color"] == "#3b82f6"
        assert body["revision"] == 1

        detail = await client.get(f"/v1/habit-goals/{body['id']}")
        assert detail.status_code == 200
        assert detail.json()["name"] == "游泳"


@pytest.mark.asyncio
async def test_list_habit_goals(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/v1/habit-goals", json={"name": "游泳"})
        await client.post("/v1/habit-goals", json={"name": "跑步", "unit": "公里"})
        resp = await client.get("/v1/habit-goals")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert {i["name"] for i in items} == {"游泳", "跑步"}


@pytest.mark.asyncio
async def test_update_habit_goal(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = (await client.post("/v1/habit-goals", json={"name": "喝水", "unit": "杯"})).json()
        resp = await client.patch(
            f"/v1/habit-goals/{created['id']}",
            json={"expectedRevision": 1, "name": "喝水打卡", "unit": "次"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "喝水打卡"
    assert body["unit"] == "次"
    assert body["revision"] == 2


@pytest.mark.asyncio
async def test_update_habit_goal_revision_conflict(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = (await client.post("/v1/habit-goals", json={"name": "喝水"})).json()
        resp = await client.patch(
            f"/v1/habit-goals/{created['id']}",
            json={"expectedRevision": 999, "name": "新名字"},
        )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REVISION_CONFLICT"


@pytest.mark.asyncio
async def test_habit_goal_not_found(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/v1/habit-goals/{uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "HABIT_GOAL_NOT_FOUND"


@pytest.mark.asyncio
async def test_two_phase_delete_habit_goal(app: FastAPI, fake_repos: dict[str, Any]) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = (await client.post("/v1/habit-goals", json={"name": "晨跑"})).json()
        goal_id = created["id"]

        preview = await client.post(
            f"/v1/habit-goals/{goal_id}/delete-preview",
            json={"expectedRevision": 1},
        )
        assert preview.status_code == 200
        confirmation_id = preview.json()["confirmationId"]

        confirm = await client.post(
            "/v1/habit-goals/delete-confirm",
            json={"confirmationId": confirmation_id},
        )
        assert confirm.status_code == 204

        detail = await client.get(f"/v1/habit-goals/{goal_id}")
        assert detail.status_code == 404


@pytest.mark.asyncio
async def test_delete_confirm_without_preview(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/habit-goals/delete-confirm",
            json={"confirmationId": str(uuid4())},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
