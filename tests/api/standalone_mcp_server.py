"""Standalone MCP Server for glasses client verification (dev-only).

用途：为 MomentOneGlasses 的 MCP 客户端（services/mcp-client.js）提供
一个无需真实 DB / Casdoor 的本地端到端验证端点。

行为与 tests/api/test_mcp_server.py 的 app fixture 一致：
- 使用 fake repositories（内存存储），工具执行不落库
- 验签器接受「QR Binding 风格」的 Server 自签 RS256 token（带 binding_id）
- 挂载路径 /mcp（与生产一致），认证失败返回 401 + WWW-Authenticate

用法：
    .venv/bin/python tests/api/standalone_mcp_server.py [--port 8765]

启动后输出一行：
    MCP_VERIFY_TOKEN=<token>

眼镜端验证脚本（MomentOneGlasses/dev/mcp-verify/verify.mjs）会读取该 token
并驱动真实客户端代码完成 发现 / 执行 / 错误链路 验证。

注意：本文件不以 test_ 开头，不会被 pytest 收集为测试用例。
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from app.core.config import Settings
from app.infrastructure.jwt.issuer import JwtIssuer
from app.modules.habit_goals.domain import HabitGoal
from app.modules.mcp import tools as mcp_tools
from app.modules.mcp.a2ui import A2UI_CATALOG_ID, A2UI_VERSION
from app.modules.mcp.endpoint import McpComponent
from app.modules.mcp.token_verifier import MomentTokenVerifier
from fastapi import FastAPI
from test_mcp_server import (  # noqa: PLC2701  # 复用同目录测试 double
    BINDING_ID,
    DEVICE_ID,
    USER_ID,
    FakeAuditRepository,
    FakeBindingSession,
    FakeHabitGoalRepository,
    FakeIdempotencyRepository,
    FakeMomentRepository,
    FakeRevisionRepository,
    FakeSession,
    _generate_rsa_keypair,  # pyright: ignore[reportPrivateUsage]
)

DEFAULT_PORT = 8765
DEFAULT_SCOPE = "moments.read moments.write"


@asynccontextmanager
async def _fake_session_factory() -> AsyncGenerator[FakeSession]:
    """工具执行用假 session（repositories 被替换后不真正使用）。"""
    yield FakeSession()


@asynccontextmanager
async def _local_binding_session_factory() -> AsyncGenerator[FakeBindingSession]:
    """用 CLI 指定的绑定记录 scope 伪造 device_bindings 查询。"""
    async with FakeBindingSession(" ".join(_local_binding_scope)) as session:
        yield session


_local_binding_scope: tuple[str, ...] = tuple(s for s in DEFAULT_SCOPE.split() if s)


def _make_settings(tmp_path: Path, *, port: int) -> Settings:
    priv_path, pub_path = _generate_rsa_keypair(tmp_path)
    return Settings(
        env="test",
        database_url="postgresql+psycopg://test:test@127.0.0.1:5432/test",
        jwt_private_key_path=str(priv_path),
        jwt_public_key_path=str(pub_path),
        jwt_issuer="https://momentone.test",
        jwt_audience="momentone-glasses",
        access_token_ttl_seconds=3600,
        refresh_token_ttl_seconds=90 * 24 * 3600,
        binding_code_ttl_seconds=300,
        binding_code_length=24,
        mcp_base_url=f"http://127.0.0.1:{port}",
        mcp_apps_html_path=None,
    )


def _install_fake_repos(fake_repos: dict[str, Any]) -> dict[str, Any]:
    """替换 mcp_tools 模块内的 repository 类（与测试 fixture 相同）。"""
    original = {
        name: getattr(mcp_tools, name)
        for name in (
            "PostgresMomentRepository",
            "SqlIdempotencyRepository",
            "SqlAuditEventRepository",
            "SqlMomentRevisionRepository",
            "SqlHabitGoalRepository",
        )
    }
    mcp_tools.PostgresMomentRepository = lambda session: fake_repos["moment"]  # type: ignore[assignment]
    mcp_tools.SqlIdempotencyRepository = lambda session: fake_repos["idempotency"]  # type: ignore[assignment]
    mcp_tools.SqlAuditEventRepository = lambda session: fake_repos["audit"]  # type: ignore[assignment]
    mcp_tools.SqlMomentRevisionRepository = lambda session: fake_repos["revision"]  # type: ignore[assignment]
    mcp_tools.SqlHabitGoalRepository = lambda session: fake_repos["habit"]  # type: ignore[assignment]
    return original


def _restore_repos(original: dict[str, Any]) -> None:
    for name, cls in original.items():
        setattr(mcp_tools, name, cls)


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone MCP server for glasses verification")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--scope",
        default=DEFAULT_SCOPE,
        help="绑定记录存储的 scope（默认 moments.read moments.write；"
        "历史冒号命名 moments:read 用于验证兼容性）",
    )
    args = parser.parse_args()

    # 绑定记录（device_bindings.scope）与签发的 token scope 一致（历史绑定
    # 冒号命名时两者都是冒号；token_verifier 以绑定记录为准并规范化）
    binding_scope: tuple[str, ...] = tuple(s for s in args.scope.split() if s)
    token_scope: tuple[str, ...] = binding_scope

    global _local_binding_scope
    _local_binding_scope = binding_scope

    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = _make_settings(Path(tmp_dir), port=args.port)

        verifier = MomentTokenVerifier(
            settings,
            session_factory=lambda: _local_binding_session_factory(),  # type: ignore[arg-type]
        )
        habit_repo = FakeHabitGoalRepository()
        now = datetime.now(UTC)
        goal = HabitGoal(
            id=UUID("33333333-3333-4333-8333-333333333333"),
            user_id=USER_ID,
            name="晨跑",
            revision=1,
            created_at=now,
            updated_at=now,
            unit="公里",
            frequency="weekly",
            times_per_week=5,
        )
        asyncio.run(habit_repo.create(goal))
        fake_repos: dict[str, Any] = {
            "moment": FakeMomentRepository(),
            "idempotency": FakeIdempotencyRepository(),
            "audit": FakeAuditRepository(),
            "revision": FakeRevisionRepository(),
            "habit": habit_repo,
        }
        original = _install_fake_repos(fake_repos)

        from app.modules.mcp.deps import McpToolEnv

        component = McpComponent(
            settings,
            verifier=verifier,
            env=McpToolEnv(session_factory=lambda: _fake_session_factory()),  # type: ignore[arg-type]
        )

        issuer = JwtIssuer(settings)
        token, _ = issuer.issue_access_token(
            binding_id=BINDING_ID,
            user_id=USER_ID,
            device_id=DEVICE_ID,
            scope=token_scope,
        )
        print(f"MCP_VERIFY_TOKEN={token}")
        print(f"MCP_VERIFY_URL=http://127.0.0.1:{args.port}/mcp")
        print(f"MCP_VERIFY_BINDING_ID={BINDING_ID}", flush=True)
        print("MCP_VERIFY_A2UI_CAPABILITY=capabilities.experimental.a2ui")
        print(f"MCP_VERIFY_A2UI_VERSION={A2UI_VERSION}")
        print(f"MCP_VERIFY_A2UI_CATALOG={A2UI_CATALOG_ID}", flush=True)

        @asynccontextmanager
        async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
            async with component.run():
                yield

        fastapi_app = FastAPI(lifespan=lifespan)
        fastapi_app.mount("/", component.asgi_app)

        try:
            uvicorn.run(fastapi_app, host="127.0.0.1", port=args.port, log_level="warning")
        finally:
            _restore_repos(original)


if __name__ == "__main__":
    main()
